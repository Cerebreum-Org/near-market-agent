"""Tests for the alignment monitor."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from near_market_agent.alignment import (
    AlignmentMonitor,
    AlignmentReport,
    Requirement,
    RequirementCheck,
)


class AlignmentMonitorTests(unittest.TestCase):
    """Test requirements extraction and alignment checking."""

    def test_extract_requirements_parses_response(self) -> None:
        mock_claude = MagicMock()
        mock_claude.create_message.return_value = json.dumps({
            "requirements": [
                {"id": "R1", "description": "Discord bot connects", "category": "core", "priority": "must"},
                {"id": "R2", "description": "Price alerts work", "category": "feature", "priority": "must"},
                {"id": "R3", "description": "README included", "category": "documentation", "priority": "should"},
            ]
        })

        monitor = AlignmentMonitor(mock_claude)
        reqs = monitor.extract_requirements("Build Discord bot", "A Discord bot with price alerts")

        self.assertEqual(len(reqs), 3)
        self.assertEqual(reqs[0].id, "R1")
        self.assertEqual(reqs[1].priority, "must")

    def test_extract_requirements_fallback_on_failure(self) -> None:
        mock_claude = MagicMock()
        mock_claude.create_message.side_effect = RuntimeError("CLI error")

        monitor = AlignmentMonitor(mock_claude)
        reqs = monitor.extract_requirements("Build something", "Description")

        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].id, "R1")

    def test_check_alignment_produces_report(self) -> None:
        mock_claude = MagicMock()
        # First call: extract requirements
        mock_claude.create_message.side_effect = [
            json.dumps({
                "requirements": [
                    {"id": "R1", "description": "API endpoint", "priority": "must"},
                    {"id": "R2", "description": "Tests pass", "priority": "must"},
                ]
            }),
            # Second call: alignment check
            json.dumps({
                "checks": [
                    {"id": "R1", "status": "pass", "evidence": "Found API endpoint"},
                    {"id": "R2", "status": "fail", "evidence": "No tests found"},
                ],
                "overall_alignment": 0.5,
                "critical_gaps": ["Tests pass"],
                "suggestions": ["Add test files"],
            }),
        ]

        monitor = AlignmentMonitor(mock_claude)
        monitor.extract_requirements("Build API", "An API with tests")
        report = monitor.check_alignment("post-build", "# API\nSome content")

        self.assertEqual(report.checkpoint, "post-build")
        self.assertEqual(len(report.checks), 2)
        self.assertEqual(report.overall_score, 0.5)
        self.assertFalse(report.passed)  # Has critical gaps
        self.assertIn("Tests pass", report.critical_gaps)

    def test_alignment_report_pass_rate(self) -> None:
        report = AlignmentReport(
            checkpoint="test",
            requirements=[],
            checks=[
                RequirementCheck(id="R1", status="pass"),
                RequirementCheck(id="R2", status="pass"),
                RequirementCheck(id="R3", status="fail"),
            ],
        )
        self.assertAlmostEqual(report.pass_rate, 2 / 3)

    def test_alignment_report_passed_property(self) -> None:
        passing = AlignmentReport(checkpoint="t", requirements=[], checks=[], critical_gaps=[])
        self.assertTrue(passing.passed)

        failing = AlignmentReport(checkpoint="t", requirements=[], checks=[], critical_gaps=["missing X"])
        self.assertFalse(failing.passed)

    def test_alignment_report_to_markdown(self) -> None:
        report = AlignmentReport(
            checkpoint="post-build",
            requirements=[
                Requirement(id="R1", description="API works", priority="must"),
            ],
            checks=[
                RequirementCheck(id="R1", status="pass", evidence="Found it"),
            ],
            overall_score=0.9,
        )
        md = report.to_markdown()
        self.assertIn("post-build", md)
        self.assertIn("✅", md)
        self.assertIn("API works", md)

    def test_alignment_report_summary(self) -> None:
        report = AlignmentReport(
            checkpoint="pre-submit",
            requirements=[],
            checks=[
                RequirementCheck(id="R1", status="pass"),
                RequirementCheck(id="R2", status="partial"),
                RequirementCheck(id="R3", status="fail"),
            ],
            overall_score=0.6,
            critical_gaps=["R3"],
        )
        summary = report.summary()
        self.assertIn("1/3 pass", summary)
        self.assertIn("1 partial", summary)
        self.assertIn("1 fail", summary)
        self.assertIn("GAPS: R3", summary)

    def test_check_alignment_skips_without_requirements(self) -> None:
        mock_claude = MagicMock()
        monitor = AlignmentMonitor(mock_claude)
        # Don't extract requirements first
        report = monitor.check_alignment("post-build", "content")

        self.assertEqual(report.overall_score, 1.0)
        self.assertTrue(report.passed)
        mock_claude.create_message.assert_not_called()
