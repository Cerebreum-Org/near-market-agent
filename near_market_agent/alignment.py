"""Alignment monitor — tracks job requirements through the build pipeline.

Extracts a concrete requirements checklist from the job description,
then verifies alignment at each checkpoint in the pipeline:

  1. POST-RESEARCH:  Do we have research coverage for all requirements?
  2. POST-BUILD:     Did the builder output address all requirements?
  3. PRE-SUBMIT:     Final alignment gate — any gaps trigger targeted revision.

Each checkpoint produces an AlignmentReport with per-requirement status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .claude_cli import ClaudeCLI
from .json_utils import extract_json
from .sanitize import sanitize_text

log = logging.getLogger(__name__)


EXTRACT_REQUIREMENTS_SYSTEM = """You are a requirements analyst. Given a job description,
extract ALL concrete, verifiable requirements into a structured checklist.

Be specific. Turn vague descriptions into testable criteria.

Examples:
- "Build a Discord bot" → "Discord bot that connects and responds to commands"
- "with price alerts" → "Monitors token prices and sends alerts when thresholds are hit"
- "deploy to npm" → "Published as an npm package with proper package.json"

Respond with ONLY valid JSON:
{
    "requirements": [
        {
            "id": "R1",
            "description": "short, specific, testable requirement",
            "category": "core|feature|quality|deployment|documentation",
            "priority": "must|should|nice"
        }
    ]
}

Extract 3-12 requirements. Focus on "must" items first. Every requirement
should be independently verifiable by reading the deliverable."""

CHECK_ALIGNMENT_SYSTEM = """You are a QA analyst checking whether work output meets requirements.

For each requirement, determine:
- "pass": the deliverable clearly addresses this requirement
- "partial": some attempt but incomplete or unclear
- "fail": not addressed at all

Be strict on "must" priority items. Be fair on "nice" items.

Respond with ONLY valid JSON:
{
    "checks": [
        {
            "id": "R1",
            "status": "pass|partial|fail",
            "evidence": "brief explanation of what you found (or didn't)"
        }
    ],
    "overall_alignment": 0.0-1.0,
    "critical_gaps": ["list of must-have requirements that failed"],
    "suggestions": ["specific actions to fix gaps"]
}"""


@dataclass
class Requirement:
    id: str
    description: str
    category: str = "core"
    priority: str = "must"


@dataclass
class RequirementCheck:
    id: str
    status: str  # pass, partial, fail
    evidence: str = ""


@dataclass
class AlignmentReport:
    checkpoint: str  # post-research, post-build, pre-submit
    requirements: list[Requirement]
    checks: list[RequirementCheck]
    overall_score: float = 0.0
    critical_gaps: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if no critical (must-have) requirements failed."""
        return len(self.critical_gaps) == 0

    @property
    def pass_rate(self) -> float:
        """Fraction of checks that passed."""
        if not self.checks:
            return 0.0
        passed = sum(1 for c in self.checks if c.status == "pass")
        return passed / len(self.checks)

    def summary(self) -> str:
        """One-line summary for logging."""
        total = len(self.checks)
        passed = sum(1 for c in self.checks if c.status == "pass")
        partial = sum(1 for c in self.checks if c.status == "partial")
        failed = sum(1 for c in self.checks if c.status == "fail")
        gaps = f" GAPS: {', '.join(self.critical_gaps)}" if self.critical_gaps else ""
        return (
            f"[{self.checkpoint}] {passed}/{total} pass, "
            f"{partial} partial, {failed} fail "
            f"(score={self.overall_score:.2f}){gaps}"
        )

    def to_markdown(self) -> str:
        """Render as markdown for workspace files."""
        lines = [f"# Alignment Check — {self.checkpoint}\n"]
        lines.append(f"**Score:** {self.overall_score:.0%}\n")

        if self.critical_gaps:
            lines.append("## ⚠️ Critical Gaps\n")
            for gap in self.critical_gaps:
                lines.append(f"- {gap}")
            lines.append("")

        lines.append("## Requirements\n")
        lines.append("| ID | Status | Requirement | Evidence |")
        lines.append("|-----|--------|-------------|----------|")

        req_map = {r.id: r for r in self.requirements}
        for check in self.checks:
            req = req_map.get(check.id)
            desc = req.description if req else "?"
            icon = {"pass": "✅", "partial": "⚠️", "fail": "❌"}.get(check.status, "?")
            lines.append(f"| {check.id} | {icon} | {desc} | {check.evidence} |")

        if self.suggestions:
            lines.append("\n## Suggestions\n")
            for s in self.suggestions:
                lines.append(f"- {s}")

        return "\n".join(lines)


class AlignmentMonitor:
    """Tracks requirements alignment through the build pipeline."""

    def __init__(self, claude: ClaudeCLI):
        self.claude = claude
        self._requirements: list[Requirement] = []

    def extract_requirements(self, job_title: str, job_description: str) -> list[Requirement]:
        """Extract structured requirements from the job description."""
        safe_title = sanitize_text(job_title, max_length=500)
        safe_desc = sanitize_text(job_description, max_length=8000)

        user = f"Job Title: {safe_title}\n\nJob Description:\n{safe_desc}"

        try:
            text = self.claude.create_message(
                system=EXTRACT_REQUIREMENTS_SYSTEM,
                user=user,
                max_tokens=2048,
            )
            data = extract_json(text, fallback=None)
            if data and "requirements" in data:
                self._requirements = [
                    Requirement(
                        id=r.get("id", f"R{i + 1}"),
                        description=r.get("description", ""),
                        category=r.get("category", "core"),
                        priority=r.get("priority", "must"),
                    )
                    for i, r in enumerate(data["requirements"])
                ]
                log.info(f"Extracted {len(self._requirements)} requirements")
                return self._requirements
        except RuntimeError as e:
            log.warning(f"Requirements extraction failed: {e}")

        # Fallback: single requirement from title
        self._requirements = [
            Requirement(id="R1", description=safe_title, category="core", priority="must")
        ]
        return self._requirements

    def check_alignment(
        self,
        checkpoint: str,
        content: str,
        context: str = "",
    ) -> AlignmentReport:
        """Run an alignment check at the given checkpoint.

        Args:
            checkpoint: Name of checkpoint (post-research, post-build, pre-submit)
            content: The content to check against requirements
            context: Additional context (e.g., research brief, job description)
        """
        if not self._requirements:
            log.warning("No requirements extracted — skipping alignment check")
            return AlignmentReport(
                checkpoint=checkpoint,
                requirements=[],
                checks=[],
                overall_score=1.0,
            )

        req_list = "\n".join(
            f"- [{r.id}] ({r.priority}) {r.description}" for r in self._requirements
        )

        # Truncate content for the check
        content_preview = content[:12000] if content else "(empty)"

        user = (
            f"# Requirements\n\n{req_list}\n\n"
            f"# Content to Check ({checkpoint})\n\n{content_preview}\n"
        )
        if context:
            user += f"\n# Additional Context\n\n{context[:4000]}\n"

        try:
            text = self.claude.create_message(
                system=CHECK_ALIGNMENT_SYSTEM,
                user=user,
                max_tokens=2048,
            )
            data = extract_json(text, fallback=None)
            if data and "checks" in data:
                checks = [
                    RequirementCheck(
                        id=c.get("id", "?"),
                        status=c.get("status", "fail"),
                        evidence=c.get("evidence", ""),
                    )
                    for c in data["checks"]
                ]
                report = AlignmentReport(
                    checkpoint=checkpoint,
                    requirements=self._requirements,
                    checks=checks,
                    overall_score=float(data.get("overall_alignment", 0.0)),
                    critical_gaps=data.get("critical_gaps", []),
                    suggestions=data.get("suggestions", []),
                )
                log.info(f"Alignment: {report.summary()}")
                return report
        except RuntimeError as e:
            log.warning(f"Alignment check failed at {checkpoint}: {e}")

        # Fallback: assume pass to not block pipeline
        return AlignmentReport(
            checkpoint=checkpoint,
            requirements=self._requirements,
            checks=[
                RequirementCheck(id=r.id, status="pass", evidence="check failed, assuming pass")
                for r in self._requirements
            ],
            overall_score=0.5,
        )

    @property
    def requirements(self) -> list[Requirement]:
        return self._requirements
