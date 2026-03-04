"""Unit tests for job router — tier classification."""

from __future__ import annotations

import unittest

from near_market_agent.job_router import classify, JobTier
from near_market_agent.models import Job


def _job(**overrides):
    base = {
        "job_id": "j-1",
        "creator_agent_id": "c-1",
        "title": "Test Job",
        "description": "A test job description.",
        "budget_amount": "5.0",
    }
    base.update(overrides)
    return Job(**base)


class ClassifyTests(unittest.TestCase):
    # --- Tier 1: Text ---

    def test_guide_is_text(self) -> None:
        result = classify(_job(title="Write a guide to NEAR staking"))
        self.assertEqual(result.tier, JobTier.TEXT)
        self.assertEqual(result.agent, "text-writer")

    def test_tutorial_is_text(self) -> None:
        result = classify(_job(title="Tutorial: NEAR Account Creation"))
        self.assertEqual(result.tier, JobTier.TEXT)

    def test_documentation_is_text(self) -> None:
        result = classify(_job(
            title="NEAR Gas Documentation",
            description="Write comprehensive documentation about NEAR gas fees.",
        ))
        self.assertEqual(result.tier, JobTier.TEXT)

    # --- Tier 2: Package ---

    def test_npm_tag_is_package(self) -> None:
        result = classify(_job(title="NEAR Helper", tags=["npm", "near"]))
        self.assertEqual(result.tier, JobTier.PACKAGE)
        self.assertEqual(result.template, "npm-package")

    def test_pypi_tag_is_package(self) -> None:
        result = classify(_job(title="NEAR Python SDK", tags=["pypi", "python"]))
        self.assertEqual(result.tier, JobTier.PACKAGE)
        self.assertEqual(result.template, "pypi-package")
        self.assertEqual(result.language, "python")

    def test_mcp_server_is_package(self) -> None:
        result = classify(_job(
            title="Build MCP Server: NEAR Wallet Operations",
            tags=["mcp", "near"],
        ))
        self.assertEqual(result.tier, JobTier.PACKAGE)
        self.assertEqual(result.template, "mcp-server")

    def test_build_title_is_package(self) -> None:
        result = classify(_job(title="Build NEAR Account Monitor"))
        self.assertEqual(result.tier, JobTier.PACKAGE)

    def test_github_action_is_package(self) -> None:
        result = classify(_job(title="NEAR Deploy Action", tags=["github-action"]))
        self.assertEqual(result.tier, JobTier.PACKAGE)

    # --- Tier 3: Service ---

    def test_discord_bot_is_service(self) -> None:
        result = classify(_job(title="Discord Bot - NEAR Price Alerts"))
        self.assertEqual(result.tier, JobTier.SERVICE)
        self.assertEqual(result.agent, "service-builder")

    def test_chrome_extension_is_service(self) -> None:
        result = classify(_job(title="Chrome Extension - NEAR Gas Tracker"))
        self.assertEqual(result.tier, JobTier.SERVICE)

    def test_telegram_bot_is_service(self) -> None:
        result = classify(_job(
            title="Build Telegram Bot for NEAR",
            description="Create a telegram bot that tracks NEAR transactions.",
        ))
        self.assertEqual(result.tier, JobTier.SERVICE)

    # --- Tier 4: System ---

    def test_multi_agent_is_system(self) -> None:
        result = classify(_job(title="Build Multi-Agent Router for NEAR"))
        self.assertEqual(result.tier, JobTier.SYSTEM)
        self.assertEqual(result.agent, "system-builder")

    def test_swarm_is_system(self) -> None:
        result = classify(_job(
            title="Agent Swarm Framework",
            description="Build orchestration for multi-agent swarm on NEAR.",
        ))
        self.assertEqual(result.tier, JobTier.SYSTEM)

    # --- Language detection ---

    def test_python_tag_detected(self) -> None:
        result = classify(_job(title="Build NEAR Tool", tags=["python"]))
        self.assertEqual(result.language, "python")

    def test_rust_in_description(self) -> None:
        result = classify(_job(
            title="Build NEAR Contract Helper",
            description="Use Rust and near-sdk-rs to build a helper tool.",
        ))
        self.assertEqual(result.language, "rust")

    def test_default_language_is_typescript(self) -> None:
        result = classify(_job(title="Build NEAR Thing"))
        self.assertEqual(result.language, "typescript")

    # --- Edge cases ---

    def test_build_guide_is_text_not_package(self) -> None:
        """'Build' in title but 'guide' too → text wins."""
        result = classify(_job(
            title="Write guide about building on NEAR",
            description="Write a comprehensive guide about building dApps on NEAR.",
        ))
        self.assertEqual(result.tier, JobTier.TEXT)

    def test_empty_description(self) -> None:
        result = classify(_job(title="Something", description=""))
        # Should not crash, defaults to text
        self.assertIn(result.tier, list(JobTier))


class RoutingResultTests(unittest.TestCase):
    def test_has_all_fields(self) -> None:
        result = classify(_job(title="Build MCP Server: Test", tags=["mcp"]))
        self.assertIsNotNone(result.tier)
        self.assertIsNotNone(result.agent)
        self.assertIsNotNone(result.language)
        self.assertIsNotNone(result.reason)
