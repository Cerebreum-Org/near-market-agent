"""Unit tests for configuration loading and validation."""

from __future__ import annotations

import os
import unittest

from near_market_agent.config import Config


class ConfigTests(unittest.TestCase):
    """Covers env parsing, defaults, and validator behavior."""

    def setUp(self) -> None:
        self._old_environ = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_environ)

    def test_from_env_parses_values(self) -> None:
        os.environ["NEAR_MARKET_API_KEY"] = "market-key"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-key"
        os.environ["NEAR_MARKET_URL"] = "https://example.test"
        os.environ["MIN_BUDGET_NEAR"] = "2.5"
        os.environ["MAX_CONCURRENT_JOBS"] = "7"
        os.environ["POLL_INTERVAL"] = "90"
        os.environ["BID_THRESHOLD"] = "0.8"
        os.environ["CLAUDE_MODEL"] = "test-model"
        os.environ["MAX_TOKENS"] = "1234"
        os.environ["DRY_RUN"] = "yes"
        os.environ["VERBOSE"] = "true"
        os.environ["LOG_DIR"] = "custom-logs"

        cfg = Config.from_env()

        self.assertEqual(cfg.market_api_key, "market-key")
        self.assertEqual(cfg.anthropic_api_key, "anthropic-key")
        self.assertEqual(cfg.market_base_url, "https://example.test")
        self.assertEqual(cfg.min_budget_near, 2.5)
        self.assertEqual(cfg.max_concurrent_jobs, 7)
        self.assertEqual(cfg.poll_interval_seconds, 90)
        self.assertEqual(cfg.bid_confidence_threshold, 0.8)
        self.assertEqual(cfg.model, "test-model")
        self.assertEqual(cfg.max_tokens, 1234)
        self.assertTrue(cfg.dry_run)
        self.assertTrue(cfg.verbose)
        self.assertEqual(cfg.log_dir, "custom-logs")
        self.assertEqual(cfg.api_url, "https://example.test/v1")

    def test_from_env_uses_safe_fallbacks_on_bad_numbers(self) -> None:
        os.environ["MIN_BUDGET_NEAR"] = "not-a-number"
        os.environ["MAX_CONCURRENT_JOBS"] = "abc"
        os.environ["POLL_INTERVAL"] = "NaN"
        os.environ["BID_THRESHOLD"] = "bad"
        os.environ["MAX_TOKENS"] = "x"

        cfg = Config.from_env()

        self.assertEqual(cfg.min_budget_near, 1.0)
        self.assertEqual(cfg.max_concurrent_jobs, 3)
        self.assertEqual(cfg.poll_interval_seconds, 60)
        self.assertEqual(cfg.bid_confidence_threshold, 0.6)
        self.assertEqual(cfg.max_tokens, 4096)

    def test_validate_returns_expected_errors(self) -> None:
        cfg = Config(
            market_api_key="",
            anthropic_api_key="",
            min_budget_near=-1,
            max_concurrent_jobs=0,
            poll_interval_seconds=0,
            bid_confidence_threshold=2.0,
            max_tokens=0,
        )

        errors = cfg.validate()

        self.assertIn("NEAR_MARKET_API_KEY not set", errors)
        # ANTHROPIC_API_KEY no longer required — using Claude CLI
        self.assertIn("MIN_BUDGET_NEAR must be >= 0", errors)
        self.assertIn("MAX_CONCURRENT_JOBS must be >= 1", errors)
        self.assertIn("POLL_INTERVAL must be >= 1", errors)
        self.assertIn("BID_THRESHOLD must be between 0 and 1", errors)
        self.assertIn("MAX_TOKENS must be >= 1", errors)
