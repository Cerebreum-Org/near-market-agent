"""Agentic work completion engine with tiered builders and 3-stage review.

Pipeline:
    Job → Route (classify tier) → Setup workspace → Run agent → Code-simplify
      → Review 1 (Requirements) → Review 2 (Quality) → Review 3 (Final Gate)
      → Package deliverable → Submit
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import Job
from .claude_cli import ClaudeCLI
from .job_router import classify, JobTier, RoutingResult


# --- Directory where templates and knowledge live ---
_PKG_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = _PKG_ROOT / "templates"
KNOWLEDGE_DIR = _PKG_ROOT / "knowledge"


# --- Review prompts (unchanged) ---

REVIEW_1_REQUIREMENTS = """You are a strict quality reviewer for freelance deliverables.

Review this deliverable against the job requirements. Check:
1. Does it address EVERY requirement listed in the job description?
2. Are there any missing sections or deliverables?
3. Is the scope sufficient for the budget?

Respond with ONLY valid JSON:
{{
  "score": <float 0.0-1.0>,
  "pass": <true if score >= 0.7>,
  "missing": ["list of missing requirements or sections"],
  "feedback": "specific actionable feedback for improvement"
}}"""

REVIEW_2_QUALITY = """You are an editorial quality reviewer.

Review this deliverable for quality, NOT requirements (those were already checked).
Check:
1. Writing quality — clear, professional, no filler or fluff
2. Technical accuracy — are claims correct? Are code examples valid?
3. Structure — logical flow, proper headings, good formatting
4. Depth — is it substantive or surface-level?

Respond with ONLY valid JSON:
{{
  "score": <float 0.0-1.0>,
  "pass": <true if score >= 0.7>,
  "issues": ["list of quality issues found"],
  "feedback": "specific actionable feedback for improvement"
}}"""

REVIEW_3_FINAL = """You are the final gatekeeper before a deliverable is submitted to a paying client.

This deliverable has already passed requirements review and quality review.
Do one final check:
1. Would YOU pay for this if you posted the job?
2. Any glaring issues that slipped through?
3. Is the length/depth proportional to the budget?

Respond with ONLY valid JSON:
{{
  "score": <float 0.0-1.0>,
  "pass": <true if score >= 0.7>,
  "verdict": "ship" or "revise",
  "feedback": "final notes — empty string if shipping"
}}"""

REVISE_SYSTEM = """You are revising a deliverable based on review feedback.
Apply the feedback precisely. Keep everything that was good. Fix what was flagged.
Output ONLY the revised deliverable — complete, not a diff."""


def _truncate_at_line(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    return text[:cut] if cut != -1 else text[:max_chars]


def _extract_json(text: str) -> dict:
    """Extract JSON from text that may contain markdown or reasoning."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return {"score": 0.5, "pass": False, "feedback": text[:500]}


@dataclass
class ReviewResult:
    stage: str
    score: float
    passed: bool
    feedback: str
    raw: dict = field(default_factory=dict)


@dataclass
class WorkResult:
    job_id: str
    content: str
    content_hash: str
    tokens_used: int = 0
    model: str = ""
    reviews: list[ReviewResult] = field(default_factory=list)
    revisions: int = 0
    tier: str = ""
    workspace_files: list[str] = field(default_factory=list)

    @property
    def preview(self) -> str:
        return self.content[:200] + ("..." if len(self.content) > 200 else "")


class WorkEngine:
    """Agentic work engine with tiered builders and 3-stage review.

    Pipeline:
        Route → Setup workspace → Run builder agent → Code-simplify
          → Review 1 → Review 2 → Review 3 → Package
    """

    MAX_REVISIONS_PER_STAGE = 2

    def __init__(self, config: Config):
        self.config = config
        self.claude = ClaudeCLI(model=config.model, max_tokens=config.max_tokens)

    # --- Workspace setup ---

    def _setup_workspace(self, job: Job, routing: RoutingResult) -> str:
        """Create a temp workspace with job description, template, and knowledge."""
        workspace = tempfile.mkdtemp(prefix=f"near_work_{routing.tier.value}_")

        # Write job description
        job_md = (
            f"# Job Requirements\n\n"
            f"**Title:** {job.title}\n"
            f"**Budget:** {job.budget_near} NEAR\n"
            f"**Tags:** {', '.join(job.tags) if job.tags else 'none'}\n\n"
            f"## Description\n\n{job.description}\n"
        )
        Path(workspace, "JOB.md").write_text(job_md)

        # Copy template if applicable
        if routing.template:
            template_dir = TEMPLATES_DIR / routing.template
            if template_dir.is_dir():
                for item in template_dir.iterdir():
                    dest = Path(workspace) / item.name
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)

        # Copy NEAR knowledge base
        near_ref = KNOWLEDGE_DIR / "near-reference.md"
        if near_ref.exists():
            shutil.copy2(near_ref, Path(workspace, "NEAR-REFERENCE.md"))

        return workspace

    def _collect_deliverable(self, workspace: str, routing: RoutingResult) -> tuple[str, list[str]]:
        """Collect the deliverable content from the workspace.

        For text jobs: return content of DELIVERABLE.md
        For code jobs: return concatenated file listing + key file contents
        """
        files = []
        for root, dirs, filenames in os.walk(workspace):
            # Skip node_modules, .git, __pycache__, dist, etc.
            dirs[:] = [d for d in dirs if d not in {
                "node_modules", ".git", "__pycache__", "dist", "build",
                ".venv", "venv", ".tox", "coverage", ".mypy_cache",
            }]
            for f in filenames:
                rel = os.path.relpath(os.path.join(root, f), workspace)
                if not rel.startswith(".") or rel in {".gitignore", ".env.example"}:
                    files.append(rel)

        # For text tier, prefer DELIVERABLE.md
        if routing.tier == JobTier.TEXT:
            deliverable_path = os.path.join(workspace, "DELIVERABLE.md")
            if os.path.exists(deliverable_path):
                content = Path(deliverable_path).read_text()
                return content, files

        # For code tiers, build a structured deliverable
        parts = [f"# Deliverable: {routing.tier.value}\n\n"]
        parts.append(f"## Files ({len(files)})\n\n")
        for f in sorted(files):
            parts.append(f"- `{f}`\n")
        parts.append("\n")

        # Include key files inline
        key_files = ["README.md", "package.json", "pyproject.toml", "src/index.ts",
                     "src/__init__.py", "Dockerfile", "manifest.json"]
        for kf in key_files:
            fp = os.path.join(workspace, kf)
            if os.path.exists(fp):
                content = Path(fp).read_text()
                parts.append(f"## `{kf}`\n\n```\n{_truncate_at_line(content, 4000)}\n```\n\n")

        # Include any other source files (up to a limit)
        char_budget = 20000
        for f in sorted(files):
            if f in key_files or f in {"JOB.md", "NEAR-REFERENCE.md"}:
                continue
            fp = os.path.join(workspace, f)
            if os.path.isfile(fp):
                try:
                    fc = Path(fp).read_text()
                except (UnicodeDecodeError, PermissionError):
                    continue
                if len(fc) > 8000:
                    fc = _truncate_at_line(fc, 8000) + "\n... (truncated)"
                entry = f"## `{f}`\n\n```\n{fc}\n```\n\n"
                if char_budget - len(entry) < 0:
                    break
                parts.append(entry)
                char_budget -= len(entry)

        return "".join(parts), files

    # --- Agent execution ---

    def _run_builder(self, job: Job, routing: RoutingResult, workspace: str) -> str:
        """Run the appropriate builder agent in the workspace."""
        prompt = (
            f"Read JOB.md for requirements. "
            f"You have NEAR-REFERENCE.md for protocol knowledge. "
        )
        if routing.template:
            prompt += f"A '{routing.template}' template is pre-loaded — build on top of it. "
        prompt += "Build the complete deliverable. Run tests if applicable."

        try:
            self.claude.run_agent(
                agent=routing.agent,
                prompt=prompt,
                workdir=workspace,
            )
        except RuntimeError:
            # Fallback: use prompt mode for text generation
            return self._fallback_generate(job, workspace, routing)

        content, files = self._collect_deliverable(workspace, routing)
        return content

    def _fallback_generate(self, job: Job, workspace: str, routing: RoutingResult) -> str:
        """Fallback to prompt-mode text generation if agentic mode fails."""
        system = (
            "You are an autonomous agent completing freelance work on market.near.ai. "
            "Produce high-quality work that exceeds expectations. "
            "Use proper markdown formatting. Be thorough and comprehensive. "
            "Output ONLY the deliverable content."
        )
        user = (
            f"Complete this job:\n\n"
            f"Title: {job.title}\n"
            f"Budget: {job.budget_near} NEAR\n"
            f"Tags: {', '.join(job.tags) if job.tags else 'none'}\n\n"
            f"Description:\n{job.description[:8000]}\n\n"
            f"Produce the complete deliverable now."
        )
        content = self.claude.create_message(system=system, user=user, max_tokens=self.config.max_tokens)

        # Write it to workspace for consistency
        Path(workspace, "DELIVERABLE.md").write_text(content)
        return content

    # --- Code simplification ---

    def _simplify(self, job: Job, workspace: str, routing: RoutingResult) -> None:
        """Run code-simplifier agent on workspace files."""
        if routing.tier == JobTier.TEXT:
            target = "DELIVERABLE.md"
        else:
            target = "all source files"

        try:
            self.claude.run_agent(
                agent="code-simplifier",
                prompt=f"Simplify {target} in this project. Keep all functionality intact.",
                workdir=workspace,
            )
        except RuntimeError:
            pass  # Simplification is optional — don't fail the job

    # --- Review pipeline ---

    def _review_prompt(self, job: Job, deliverable: str) -> str:
        return (
            f"JOB TITLE: {job.title}\n"
            f"BUDGET: {job.budget_near} NEAR\n"
            f"REQUIREMENTS:\n{job.description[:4000]}\n\n"
            f"---\n\n"
            f"DELIVERABLE:\n{_truncate_at_line(deliverable, 12000)}"
        )

    def _run_review(self, stage: str, system: str, job: Job, deliverable: str) -> ReviewResult:
        raw_response = self.claude.create_message(
            system=system,
            user=self._review_prompt(job, deliverable),
            max_tokens=1024,
        )
        parsed = _extract_json(raw_response)
        score = float(parsed.get("score", 0.5))
        passed = bool(parsed.get("pass", False)) or parsed.get("verdict") == "ship"
        feedback = parsed.get("feedback", "") or ""
        if not passed and not feedback:
            issues = parsed.get("missing", []) or parsed.get("issues", [])
            if issues:
                feedback = "; ".join(str(i) for i in issues)

        return ReviewResult(stage=stage, score=score, passed=passed, feedback=feedback, raw=parsed)

    def _revise(self, job: Job, deliverable: str, feedback: str) -> str:
        prompt = (
            f"ORIGINAL JOB:\n{job.title}\n{job.description[:4000]}\n\n"
            f"CURRENT DELIVERABLE:\n{_truncate_at_line(deliverable, 8000)}\n\n"
            f"REVIEW FEEDBACK:\n{feedback}\n\n"
            f"Revise the deliverable to address all feedback. Output the complete revised version."
        )
        return self.claude.create_message(
            system=REVISE_SYSTEM, user=prompt, max_tokens=self.config.max_tokens,
        )

    def _run_stage(
        self, stage_name: str, system_prompt: str, job: Job,
        deliverable: str, reviews: list[ReviewResult],
    ) -> tuple[str, bool]:
        for attempt in range(1 + self.MAX_REVISIONS_PER_STAGE):
            review = self._run_review(stage_name, system_prompt, job, deliverable)
            reviews.append(review)
            if review.passed:
                return deliverable, True
            if attempt < self.MAX_REVISIONS_PER_STAGE:
                deliverable = self._revise(job, deliverable, review.feedback)
        return deliverable, False

    # --- Main pipeline ---

    def complete_job(self, job: Job) -> WorkResult:
        """Complete a job through the full agentic pipeline."""
        # Step 1: Route
        routing = classify(job)

        # Step 2: Setup workspace
        workspace = self._setup_workspace(job, routing)

        try:
            # Step 3: Run builder agent
            content = self._run_builder(job, routing, workspace)

            # Step 4: Code-simplify
            self._simplify(job, workspace, routing)

            # Re-collect after simplification (files may have changed)
            if routing.tier != JobTier.TEXT or not content:
                content, files = self._collect_deliverable(workspace, routing)
            else:
                deliverable_path = os.path.join(workspace, "DELIVERABLE.md")
                if os.path.exists(deliverable_path):
                    content = Path(deliverable_path).read_text()
                files = []

            # Step 5: Review pipeline
            reviews: list[ReviewResult] = []
            total_revisions = 0

            content, _ = self._run_stage(
                "requirements", REVIEW_1_REQUIREMENTS, job, content, reviews,
            )
            total_revisions += sum(1 for r in reviews if not r.passed)

            content, _ = self._run_stage(
                "quality", REVIEW_2_QUALITY, job, content, reviews,
            )
            total_revisions += sum(
                1 for r in reviews if r.stage == "quality" and not r.passed
            )

            content, _ = self._run_stage(
                "final", REVIEW_3_FINAL, job, content, reviews,
            )
            total_revisions += sum(
                1 for r in reviews if r.stage == "final" and not r.passed
            )

            return WorkResult(
                job_id=job.job_id,
                content=content,
                content_hash=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
                model=self.config.model,
                reviews=reviews,
                revisions=total_revisions,
                tier=routing.tier.value,
                workspace_files=files if routing.tier != JobTier.TEXT else [],
            )
        finally:
            # Cleanup workspace
            shutil.rmtree(workspace, ignore_errors=True)

    def handle_revision(self, job: Job, original: str, feedback: str) -> WorkResult:
        """Handle a revision request from the requester."""
        revised = self._revise(job, original, feedback)

        reviews: list[ReviewResult] = []
        total_revisions = 0

        revised, _ = self._run_stage(
            "requirements", REVIEW_1_REQUIREMENTS, job, revised, reviews,
        )
        total_revisions += sum(1 for r in reviews if not r.passed)

        revised, _ = self._run_stage(
            "quality", REVIEW_2_QUALITY, job, revised, reviews,
        )
        total_revisions += sum(
            1 for r in reviews if r.stage == "quality" and not r.passed
        )

        revised, _ = self._run_stage(
            "final", REVIEW_3_FINAL, job, revised, reviews,
        )
        total_revisions += sum(
            1 for r in reviews if r.stage == "final" and not r.passed
        )

        return WorkResult(
            job_id=job.job_id,
            content=revised,
            content_hash=f"sha256:{hashlib.sha256(revised.encode()).hexdigest()}",
            model=self.config.model,
            reviews=reviews,
            revisions=total_revisions,
        )

    async def complete_job_async(self, job: Job) -> WorkResult:
        return await asyncio.to_thread(self.complete_job, job)

    async def handle_revision_async(self, job: Job, original: str, feedback: str) -> WorkResult:
        return await asyncio.to_thread(self.handle_revision, job, original, feedback)
