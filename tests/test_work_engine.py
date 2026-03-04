"""Unit tests for the agentic work completion engine."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch, MagicMock

from near_market_agent.config import Config
from near_market_agent.models import Job
from near_market_agent.work_engine import WorkEngine, WorkResult
from near_market_agent.json_utils import extract_json as _extract_json


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
            engine._run_builder = lambda job, routing, ws: "# Guide\nAsync Python is great."
            engine._simplify = lambda job, ws, routing: None
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
            engine._run_builder = lambda job, routing, ws: "# Draft\nWeak content."
            engine._simplify = lambda job, ws, routing: None
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
            engine._run_builder = lambda job, routing, ws: "# Package\nBuilt."
            engine._simplify = lambda job, ws, routing: None
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
            engine._run_builder = lambda job, routing, ws: ""
            engine._simplify = lambda job, ws, routing: None
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
