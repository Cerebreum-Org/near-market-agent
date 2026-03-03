"""Unit tests for the work completion engine."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from near_market_agent.config import Config
from near_market_agent.models import Job
from near_market_agent.work_engine import WorkEngine, WorkResult


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


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        del kwargs
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


class WorkEngineTests(unittest.TestCase):
    def test_complete_job_returns_result_with_hash(self) -> None:
        usage = SimpleNamespace(input_tokens=100, output_tokens=200)
        response = SimpleNamespace(
            content=[SimpleNamespace(text="# Guide\nAsync Python is great.")],
            usage=usage,
        )
        cfg = Config(market_api_key="m", anthropic_api_key="a")

        with patch(
            "near_market_agent.work_engine.anthropic.Anthropic",
            return_value=_FakeAnthropicClient(response),
        ):
            engine = WorkEngine(cfg)
            result = engine.complete_job(_job())

        self.assertEqual(result.job_id, "job-1")
        self.assertIn("Async Python", result.content)
        self.assertTrue(result.content_hash.startswith("sha256:"))
        self.assertEqual(result.tokens_used, 300)
        self.assertEqual(result.preview, result.content[:200])

    def test_complete_job_raises_on_empty_response(self) -> None:
        response = SimpleNamespace(content=[], usage=None)
        cfg = Config(market_api_key="m", anthropic_api_key="a")

        with patch(
            "near_market_agent.work_engine.anthropic.Anthropic",
            return_value=_FakeAnthropicClient(response),
        ):
            engine = WorkEngine(cfg)
            with self.assertRaises(RuntimeError) as ctx:
                engine.complete_job(_job())
            self.assertIn("Empty response", str(ctx.exception))

    def test_handle_revision_includes_feedback(self) -> None:
        usage = SimpleNamespace(input_tokens=150, output_tokens=250)
        response = SimpleNamespace(
            content=[SimpleNamespace(text="# Revised Guide\nNow with more detail.")],
            usage=usage,
        )
        cfg = Config(market_api_key="m", anthropic_api_key="a")

        with patch(
            "near_market_agent.work_engine.anthropic.Anthropic",
            return_value=_FakeAnthropicClient(response),
        ):
            engine = WorkEngine(cfg)
            result = engine.handle_revision(
                _job(), "Original content", "Need more examples"
            )

        self.assertIn("Revised Guide", result.content)
        self.assertEqual(result.tokens_used, 400)

    def test_work_result_preview_truncates(self) -> None:
        r = WorkResult(job_id="j1", content="x" * 300, content_hash="sha256:abc")
        self.assertEqual(len(r.preview), 203)  # 200 + "..."
        self.assertTrue(r.preview.endswith("..."))

        short = WorkResult(job_id="j2", content="short", content_hash="sha256:def")
        self.assertEqual(short.preview, "short")
