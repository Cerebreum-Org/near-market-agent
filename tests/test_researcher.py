"""Tests for the deep research module."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from near_market_agent.researcher import (
    ResearchBrief,
    Researcher,
)


class ResearcherTests(unittest.TestCase):
    """Test the research pipeline."""

    def test_extract_topics_parses_llm_response(self) -> None:
        """LLM topic extraction returns structured data."""
        mock_claude = MagicMock()
        mock_claude.create_message.return_value = json.dumps(
            {
                "search_queries": ["NEAR Protocol SDK docs", "MCP server tutorial"],
                "packages": {"npm": ["near-api-js"], "pypi": ["near-sdk"]},
                "doc_urls": ["https://docs.near.org"],
                "key_technologies": ["NEAR", "MCP"],
            }
        )

        researcher = Researcher(mock_claude)
        topics = researcher._extract_topics("Build MCP server", "Create an MCP server for NEAR")

        self.assertEqual(len(topics["search_queries"]), 2)
        self.assertIn("near-api-js", topics["packages"]["npm"])

    def test_extract_topics_fallback_on_failure(self) -> None:
        """Falls back to regex extraction when LLM fails."""
        mock_claude = MagicMock()
        mock_claude.create_message.side_effect = RuntimeError("CLI error")

        researcher = Researcher(mock_claude)
        topics = researcher._extract_topics(
            "Build Discord bot for NEAR",
            "Create a Discord bot that monitors NEAR blockchain",
        )

        # Should find discord and near patterns
        self.assertTrue(any("discord" in q.lower() for q in topics["search_queries"]))
        self.assertTrue(any("near" in q.lower() for q in topics["search_queries"]))

    def test_fallback_extract_catches_common_techs(self) -> None:
        """Regex fallback identifies common technology names."""
        mock_claude = MagicMock()
        researcher = Researcher(mock_claude)

        topics = researcher._fallback_extract(
            "Stripe + Supabase integration",
            "Build a payment system using Stripe API with Supabase backend and React frontend",
        )

        queries = " ".join(topics["search_queries"]).lower()
        self.assertIn("stripe", queries)
        self.assertIn("supabase", queries)
        self.assertIn("react", queries)

    @patch("near_market_agent.researcher._run_web_search")
    @patch("near_market_agent.researcher._lookup_npm_package")
    def test_research_job_produces_brief(self, mock_npm, mock_search) -> None:
        """Full research pipeline produces a brief with content."""
        mock_search.return_value = [
            {"title": "NEAR Docs", "url": "https://docs.near.org", "snippet": "NEAR SDK guide"},
        ]
        mock_npm.return_value = "**near-api-js** v5.0.0: NEAR JavaScript SDK"

        mock_claude = MagicMock()
        mock_claude.create_message.side_effect = [
            # First call: extract topics
            json.dumps(
                {
                    "search_queries": ["NEAR SDK docs"],
                    "packages": {"npm": ["near-api-js"], "pypi": []},
                    "doc_urls": [],
                    "key_technologies": ["NEAR"],
                }
            ),
            # Second call: synthesize
            "# Research Brief\n\nNEAR API documentation and patterns...",
        ]

        researcher = Researcher(mock_claude)
        brief = researcher.research_job("Build NEAR tool", "Create a NEAR blockchain tool")

        self.assertIsInstance(brief, ResearchBrief)
        self.assertIn("Research Brief", brief.content)
        self.assertTrue(len(brief.content) > 10)

    def test_research_brief_dataclass(self) -> None:
        """ResearchBrief holds structured data."""
        brief = ResearchBrief(
            content="# Brief\nSome research",
            sources=["https://example.com"],
            search_queries=["test query"],
            packages_found=["npm/test-pkg"],
        )
        self.assertEqual(len(brief.sources), 1)
        self.assertEqual(len(brief.packages_found), 1)

    @patch("near_market_agent.researcher._run_web_search")
    def test_research_with_no_results(self, mock_search) -> None:
        """Research produces a fallback brief when no search results found."""
        mock_search.return_value = []

        mock_claude = MagicMock()
        mock_claude.create_message.return_value = json.dumps(
            {
                "search_queries": ["obscure thing"],
                "packages": {"npm": [], "pypi": []},
                "doc_urls": [],
                "key_technologies": ["something rare"],
            }
        )

        researcher = Researcher(mock_claude)
        brief = researcher.research_job("Build something rare", "Very niche job description")

        self.assertIsInstance(brief, ResearchBrief)
        self.assertTrue(len(brief.content) > 0)
        # Should contain fallback text
        self.assertIn("Research Brief", brief.content)
