"""Unit tests for the agentic work completion engine."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch, MagicMock

from near_market_agent.config import Config
from near_market_agent.models import Job
from near_market_agent.alignment import AlignmentReport, Requirement, RequirementCheck
from near_market_agent.work_engine import WorkEngine, WorkResult, ExecutionResult
from near_market_agent.researcher import ResearchBrief
from near_market_agent.json_utils import extract_json as _extract_json

_NO_TESTS = ExecutionResult(passed=True, framework="none", output="No tests")


def _mock_alignment(engine):
    """Mock out alignment monitor for unit tests."""
    engine.alignment.extract_requirements = lambda t, d: [
        Requirement(id="R1", description="test", priority="must")
    ]
    engine.alignment._requirements = [Requirement(id="R1", description="test", priority="must")]
    engine.alignment.check_alignment = lambda cp, content, context="": AlignmentReport(
        checkpoint=cp, requirements=[], checks=[
            RequirementCheck(id="R1", status="pass", evidence="ok")
        ], overall_score=0.9,
    )


def _job(**overrides):
    base = {
        "job_id": "job-1",
        "creator_agent_id": "creator-1",
        "title": "Write a technical guide",
        "description": "Produce a comprehensive technical guide on async Python.",
        "budget_amount": "5.0",
    }
    base.update(overrides)
    return Job(**base)


PASSING_REVIEW = json.dumps({"score": 0.9, "pass": True, "feedback": ""})
FAILING_REVIEW = json.dumps({
    "score": 0.4, "pass": False,
    "feedback": "Missing examples section",
})


class WorkEngineTests(unittest.TestCase):
    def test_complete_job_all_reviews_pass(self) -> None:
        """Agentic build + 3 passing reviews = ship."""
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            # Reviews only — builder and simplifier are mocked
            mock_claude.create_message.side_effect = [
                PASSING_REVIEW,
                PASSING_REVIEW,
                PASSING_REVIEW,
            ]
            engine = WorkEngine(cfg)
            # Mock out the agentic parts
            engine.researcher.research_job = lambda t, d: ResearchBrief(content="", sources=[])
            _mock_alignment(engine)
            engine._run_builder = lambda job, routing, ws: "# Guide\nAsync Python is great."
            engine._simplify = lambda job, ws, routing: None
            engine._validate_execution = lambda ws, routing: _NO_TESTS
            engine._publish_if_needed = lambda job, routing, ws: []
            result = engine.complete_job(_job())

        self.assertEqual(result.job_id, "job-1")
        self.assertIn("Async Python", result.content)
        self.assertTrue(result.content_hash.startswith("sha256:"))
        self.assertEqual(result.revisions, 0)
        self.assertEqual(len(result.reviews), 3)
        self.assertTrue(all(r.passed for r in result.reviews))
        self.assertEqual(result.tier, "text")

    def test_complete_job_revision_on_failed_review(self) -> None:
        """Failed review triggers revision, then passes on retry."""
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = [
                FAILING_REVIEW,                     # review 1 fails
                "# Revised\nBetter content.",       # revision
                PASSING_REVIEW,                     # review 1 passes
                PASSING_REVIEW,                     # review 2 passes
                PASSING_REVIEW,                     # review 3 passes
            ]
            engine = WorkEngine(cfg)
            engine.researcher.research_job = lambda t, d: ResearchBrief(content="", sources=[])
            _mock_alignment(engine)
            engine._run_builder = lambda job, routing, ws: "# Draft\nWeak content."
            engine._simplify = lambda job, ws, routing: None
            engine._validate_execution = lambda ws, routing: _NO_TESTS
            engine._publish_if_needed = lambda job, routing, ws: []
            result = engine.complete_job(_job())

        self.assertIn("Better content", result.content)
        self.assertGreater(result.revisions, 0)
        self.assertEqual(len(result.reviews), 4)

    def test_complete_job_package_tier(self) -> None:
        """Package job routes to package-builder."""
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = [
                PASSING_REVIEW,
                PASSING_REVIEW,
                PASSING_REVIEW,
            ]
            engine = WorkEngine(cfg)
            engine.researcher.research_job = lambda t, d: ResearchBrief(content="", sources=[])
            _mock_alignment(engine)
            engine._run_builder = lambda job, routing, ws: "# Package\nBuilt."
            engine._simplify = lambda job, ws, routing: None
            engine._validate_execution = lambda ws, routing: _NO_TESTS
            engine._publish_if_needed = lambda job, routing, ws: []
            result = engine.complete_job(_job(
                title="Build NEAR Account Monitor",
                tags=["npm", "near"],
            ))

        self.assertEqual(result.tier, "package")

    def test_complete_job_empty_builder_still_produces_result(self) -> None:
        """Empty builder output → collector picks up workspace files, reviews still run."""
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            engine = WorkEngine(cfg)
            engine.researcher.research_job = lambda t, d: ResearchBrief(content="", sources=[])
            _mock_alignment(engine)
            engine._run_builder = lambda job, routing, ws: ""
            engine._simplify = lambda job, ws, routing: None
            engine._validate_execution = lambda ws, routing: _NO_TESTS
            engine._publish_if_needed = lambda job, routing, ws: []
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = [
                PASSING_REVIEW, PASSING_REVIEW, PASSING_REVIEW,
            ]
            result = engine.complete_job(_job())
            # Even with empty builder, collector finds JOB.md etc.
            self.assertIsNotNone(result.content)
            self.assertEqual(len(result.reviews), 3)

    def test_handle_revision_runs_full_review_pipeline(self) -> None:
        """Requester revision → revise → 3 review stages."""
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = [
                "# Revised Guide\nNow with more detail.",  # revision
                PASSING_REVIEW,                              # review 1
                PASSING_REVIEW,                              # review 2
                PASSING_REVIEW,                              # review 3
            ]
            engine = WorkEngine(cfg)
            result = engine.handle_revision(
                _job(), "Original content", "Need more examples"
            )

        self.assertIn("Revised Guide", result.content)
        self.assertEqual(len(result.reviews), 3)

    def test_work_result_preview_truncates(self) -> None:
        r = WorkResult(job_id="j1", content="x" * 300, content_hash="sha256:abc")
        self.assertEqual(len(r.preview), 203)
        self.assertTrue(r.preview.endswith("..."))

        short = WorkResult(job_id="j2", content="short", content_hash="sha256:def")
        self.assertEqual(short.preview, "short")

    def test_work_result_has_tier_and_files(self) -> None:
        r = WorkResult(
            job_id="j1", content="x", content_hash="sha256:abc",
            tier="package", workspace_files=["src/index.ts", "package.json"],
        )
        self.assertEqual(r.tier, "package")
        self.assertEqual(len(r.workspace_files), 2)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        result = _extract_json('{"score": 0.8, "pass": true}')
        self.assertEqual(result["score"], 0.8)

    def test_json_in_code_block(self) -> None:
        text = 'Here is my review:\n```json\n{"score": 0.9, "pass": true}\n```'
        result = _extract_json(text)
        self.assertEqual(result["score"], 0.9)

    def test_json_with_surrounding_text(self) -> None:
        text = 'After careful review: {"score": 0.7, "pass": true, "feedback": "good"} end.'
        result = _extract_json(text)
        self.assertEqual(result["score"], 0.7)

    def test_fallback_on_garbage(self) -> None:
        result = _extract_json("no json here at all")
        self.assertIn("feedback", result)
        self.assertFalse(result["pass"])


class ExecutionValidationTests(unittest.TestCase):
    """Tests for the execution validation step."""

    def test_text_tier_skips_execution(self) -> None:
        """Text jobs skip execution validation."""
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            from near_market_agent.job_router import RoutingResult, JobTier
            routing = RoutingResult(tier=JobTier.TEXT, agent="text-writer", template=None, language="markdown", reason="text")
            result = engine._validate_execution("/tmp/fake", routing)
        self.assertTrue(result.passed)
        self.assertEqual(result.framework, "none")

    def test_npm_tests_pass(self) -> None:
        """npm test returning 0 → passed."""
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            import tempfile
            ws = tempfile.mkdtemp()
            try:
                # Create package.json with a passing test
                import json as _json
                _json.dump({"name": "test", "scripts": {"test": "echo '5 passed'"}}, open(os.path.join(ws, "package.json"), "w"))
                from near_market_agent.job_router import RoutingResult, JobTier
                routing = RoutingResult(tier=JobTier.PACKAGE, agent="package-builder", template=None, language="typescript", reason="npm")
                result = engine._validate_execution(ws, routing)
                self.assertTrue(result.passed)
                self.assertEqual(result.framework, "npm")
            finally:
                import shutil
                shutil.rmtree(ws, ignore_errors=True)

    def test_parse_pytest_output(self) -> None:
        """Parse pytest-style output."""
        total, failed = WorkEngine._parse_test_counts("3 passed, 1 failed in 0.5s")
        self.assertEqual(total, 4)
        self.assertEqual(failed, 1)

    def test_parse_jest_output(self) -> None:
        """Parse jest-style output."""
        total, failed = WorkEngine._parse_test_counts("Tests: 2 failed, 5 passed, 7 total")
        self.assertEqual(total, 7)
        self.assertEqual(failed, 2)

    def test_parse_cargo_output(self) -> None:
        """Parse cargo-style output."""
        total, failed = WorkEngine._parse_test_counts("test result: ok. 10 passed; 0 failed")
        self.assertEqual(total, 10)
        self.assertEqual(failed, 0)

    def test_parse_no_tests(self) -> None:
        """No recognizable output → 0/0."""
        total, failed = WorkEngine._parse_test_counts("some random output")
        self.assertEqual(total, 0)
        self.assertEqual(failed, 0)

    def test_execution_result_summary(self) -> None:
        r = ExecutionResult(passed=True, framework="npm", output="ok", test_count=5, fail_count=0)
        self.assertIn("PASSED", r.summary())
        self.assertIn("npm", r.summary())

        r2 = ExecutionResult(passed=False, framework="pytest", output="fail", test_count=3, fail_count=1)
        self.assertIn("FAILED", r2.summary())


class PublishTests(unittest.TestCase):
    """Tests for the publish step."""

    def test_needs_publish_npm_tag(self) -> None:
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            from near_market_agent.job_router import RoutingResult, JobTier
            routing = RoutingResult(tier=JobTier.PACKAGE, agent="package-builder", template=None, language="typescript", reason="npm")
            job = _job(tags=["npm", "publish"])
            self.assertTrue(engine._needs_publish(job, routing))

    def test_needs_publish_description_keyword(self) -> None:
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            from near_market_agent.job_router import RoutingResult, JobTier
            routing = RoutingResult(tier=JobTier.PACKAGE, agent="package-builder", template=None, language="typescript", reason="npm")
            job = _job(description="Please publish to npm registry")
            self.assertTrue(engine._needs_publish(job, routing))

    def test_no_publish_for_text_tier(self) -> None:
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            from near_market_agent.job_router import RoutingResult, JobTier
            routing = RoutingResult(tier=JobTier.TEXT, agent="text-writer", template=None, language="markdown", reason="text")
            job = _job(tags=["npm", "publish"])
            self.assertFalse(engine._needs_publish(job, routing))

    def test_no_publish_without_keywords(self) -> None:
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            from near_market_agent.job_router import RoutingResult, JobTier
            routing = RoutingResult(tier=JobTier.PACKAGE, agent="package-builder", template=None, language="typescript", reason="npm")
            job = _job(tags=["near"], description="Build a tool")
            self.assertFalse(engine._needs_publish(job, routing))


class CostAwarePipelineTests(unittest.TestCase):
    """Tests for lightweight vs full pipeline."""

    def test_lightweight_threshold(self) -> None:
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI"):
            engine = WorkEngine(cfg)
            cheap_job = _job(budget_amount="2.0")
            expensive_job = _job(budget_amount="5.0")
            self.assertTrue(engine._is_lightweight(cheap_job))
            self.assertFalse(engine._is_lightweight(expensive_job))

    def test_lightweight_pipeline_sets_cost_tier(self) -> None:
        """Cheap job → cost_tier='lightweight'."""
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = [
                PASSING_REVIEW, PASSING_REVIEW, PASSING_REVIEW,
            ]
            engine = WorkEngine(cfg)
            engine.researcher.research_job = lambda t, d: ResearchBrief(content="", sources=[])
            _mock_alignment(engine)
            engine._run_builder = lambda job, routing, ws: "# Guide\nContent."
            engine._simplify = lambda job, ws, routing: None
            engine._validate_execution = lambda ws, routing: _NO_TESTS
            engine._publish_if_needed = lambda job, routing, ws: []
            result = engine.complete_job(_job(budget_amount="1.5"))
        self.assertEqual(result.cost_tier, "lightweight")

    def test_full_pipeline_sets_cost_tier(self) -> None:
        """Expensive job → cost_tier='full'."""
        cfg = Config(market_api_key="m")
        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = [
                PASSING_REVIEW, PASSING_REVIEW, PASSING_REVIEW,
            ]
            engine = WorkEngine(cfg)
            engine.researcher.research_job = lambda t, d: ResearchBrief(content="", sources=[])
            _mock_alignment(engine)
            engine._run_builder = lambda job, routing, ws: "# Guide\nContent."
            engine._simplify = lambda job, ws, routing: None
            engine._validate_execution = lambda ws, routing: _NO_TESTS
            engine._publish_if_needed = lambda job, routing, ws: []
            result = engine.complete_job(_job(budget_amount="10.0"))
        self.assertEqual(result.cost_tier, "full")

    def test_work_result_to_dict_includes_new_fields(self) -> None:
        """WorkResult.to_dict() includes execution, cost_tier, publish_artifacts."""
        r = WorkResult(
            job_id="j1", content="x", content_hash="sha256:abc",
            execution_result=ExecutionResult(passed=True, framework="npm", output="ok", test_count=5, fail_count=0),
            publish_artifacts=["my-pkg-1.0.0.tgz"],
            cost_tier="full",
        )
        d = r.to_dict()
        self.assertEqual(d["cost_tier"], "full")
        self.assertEqual(d["publish_artifacts"], ["my-pkg-1.0.0.tgz"])
        self.assertTrue(d["execution"]["passed"])
        self.assertEqual(d["execution"]["framework"], "npm")
        self.assertEqual(d["execution"]["test_count"], 5)
