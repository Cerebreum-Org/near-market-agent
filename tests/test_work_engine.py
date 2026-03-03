"""Unit tests for the work completion engine."""

from __future__ import annotations

import unittest
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


class WorkEngineTests(unittest.TestCase):
    def test_complete_job_returns_result_with_hash(self) -> None:
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.return_value = "# Guide\nAsync Python is great."
            engine = WorkEngine(cfg)
            result = engine.complete_job(_job())

        self.assertEqual(result.job_id, "job-1")
        self.assertIn("Async Python", result.content)
        self.assertTrue(result.content_hash.startswith("sha256:"))
        self.assertEqual(result.tokens_used, 0)  # CLI doesn't report tokens
        self.assertEqual(result.preview, result.content[:200])

    def test_complete_job_raises_on_empty_response(self) -> None:
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_message.return_value = ""
            engine = WorkEngine(cfg)
            with self.assertRaises(RuntimeError) as ctx:
                engine.complete_job(_job())
            self.assertIn("Empty response", str(ctx.exception))

    def test_handle_revision_includes_feedback(self) -> None:
        cfg = Config(market_api_key="m")

        with patch("near_market_agent.work_engine.ClaudeCLI") as MockCLI:
            mock_claude = MockCLI.return_value
            mock_claude.create_conversation.return_value = "# Revised Guide\nNow with more detail."
            engine = WorkEngine(cfg)
            result = engine.handle_revision(
                _job(), "Original content", "Need more examples"
            )

        self.assertIn("Revised Guide", result.content)
        # Verify conversation was called with the right messages
        call_args = mock_claude.create_conversation.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages") or call_args[0][1]
        self.assertEqual(len(messages), 3)
        self.assertIn("Need more examples", messages[2]["content"])

    def test_work_result_preview_truncates(self) -> None:
        r = WorkResult(job_id="j1", content="x" * 300, content_hash="sha256:abc")
        self.assertEqual(len(r.preview), 203)  # 200 + "..."
        self.assertTrue(r.preview.endswith("..."))

        short = WorkResult(job_id="j2", content="short", content_hash="sha256:def")
        self.assertEqual(short.preview, "short")
