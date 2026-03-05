"""Unit tests for LLM job evaluation logic."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from near_market_agent.config import Config
from near_market_agent.job_evaluator import JobEvaluator
from near_market_agent.models import Job


def _job(**overrides):
    base = {
        "job_id": "job-1",
        "creator_agent_id": "creator-1",
        "title": "Write NEAR ecosystem summary",
        "description": "Research and summarize key ecosystem updates.",
        "budget_amount": "10.0",
        "bid_count": 3,
    }
    base.update(overrides)
    return Job(**base)


class JobEvaluatorTests(unittest.TestCase):
    def test_preflight_filter_skips_low_budget_without_llm_call(self) -> None:
        cfg = Config(market_api_key="m", min_budget_near=5.0)

        with patch("near_market_agent.job_evaluator.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job(budget_amount="2.0"))

        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.should_bid)
        self.assertEqual(result.category, "skip")
        self.assertIn("Budget too low", result.reasoning)
        mock_claude.create_message.assert_not_called()

    def test_evaluate_job_parses_fenced_json_and_normalizes_fields(self) -> None:
        payload = {
            "score": 4.2,
            "should_bid": 1,
            "reasoning": "Strong fit",
            "suggested_bid_amount": "-5",
            "suggested_eta_hours": "8",
            "proposal_draft": "I can complete this quickly.",
            "category": "research",
        }
        text = f"```json\n{json.dumps(payload)}\n```"
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.job_evaluator.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.return_value = text
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job())

        self.assertEqual(result.score, 1.0)
        self.assertTrue(result.should_bid)
        self.assertEqual(result.reasoning, "Strong fit")
        self.assertIsNone(result.suggested_bid_amount)
        self.assertEqual(result.suggested_eta_hours, 8)
        self.assertEqual(result.category, "research")

    def test_evaluate_job_handles_invalid_json(self) -> None:
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.job_evaluator.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.return_value = "not json"
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job())

        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.should_bid)
        self.assertEqual(result.category, "skip")
        self.assertIn("Failed to parse", result.reasoning)

    def test_evaluate_job_handles_cli_error(self) -> None:
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.job_evaluator.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.side_effect = RuntimeError(
                "claude CLI failed (exit 1): error"
            )
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job())

        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.should_bid)
        self.assertEqual(result.category, "skip")
        self.assertIn("Claude CLI error", result.reasoning)

    def test_extract_text_joins_text_blocks(self) -> None:
        response = SimpleNamespace(
            content=[
                SimpleNamespace(text="first"),
                SimpleNamespace(text="second"),
                SimpleNamespace(foo="x"),
            ]
        )
        from near_market_agent import extract_llm_text

        self.assertEqual(extract_llm_text(response), "first\nsecond")
