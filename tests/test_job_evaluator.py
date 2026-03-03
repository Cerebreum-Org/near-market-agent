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
        "budget_amount": "4.0",
        "bid_count": 3,
    }
    base.update(overrides)
    return Job(**base)


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def create(self, **kwargs):
        del kwargs
        self.calls += 1
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


class JobEvaluatorTests(unittest.TestCase):
    def test_preflight_filter_skips_low_budget_without_llm_call(self) -> None:
        fake_response = SimpleNamespace(content=[SimpleNamespace(text="unused")])
        fake_client = _FakeAnthropicClient(fake_response)
        cfg = Config(market_api_key="m", anthropic_api_key="a", min_budget_near=5.0)

        with patch("near_market_agent.job_evaluator.anthropic.Anthropic", return_value=fake_client):
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job(budget_amount="2.0"))

        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.should_bid)
        self.assertEqual(result.category, "skip")
        self.assertIn("Budget too low", result.reasoning)
        self.assertEqual(fake_client.messages.calls, 0)

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
        response = SimpleNamespace(content=[SimpleNamespace(text=text)])
        fake_client = _FakeAnthropicClient(response)
        cfg = Config(market_api_key="m", anthropic_api_key="a")

        with patch("near_market_agent.job_evaluator.anthropic.Anthropic", return_value=fake_client):
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job())

        self.assertEqual(result.score, 1.0)
        self.assertTrue(result.should_bid)
        self.assertEqual(result.reasoning, "Strong fit")
        self.assertIsNone(result.suggested_bid_amount)
        self.assertEqual(result.suggested_eta_hours, 8)
        self.assertEqual(result.category, "research")

    def test_evaluate_job_handles_invalid_json(self) -> None:
        response = SimpleNamespace(content=[SimpleNamespace(text="not json")])
        cfg = Config(market_api_key="m", anthropic_api_key="a")

        with patch(
            "near_market_agent.job_evaluator.anthropic.Anthropic",
            return_value=_FakeAnthropicClient(response),
        ):
            evaluator = JobEvaluator(cfg)
            result = evaluator.evaluate_job(_job())

        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.should_bid)
        self.assertEqual(result.category, "skip")
        self.assertIn("Failed to parse", result.reasoning)

    def test_extract_text_joins_text_blocks(self) -> None:
        response = SimpleNamespace(
            content=[SimpleNamespace(text="first"), SimpleNamespace(text="second"), SimpleNamespace(foo="x")]
        )
        self.assertEqual(JobEvaluator._extract_text(response), "first\nsecond")

