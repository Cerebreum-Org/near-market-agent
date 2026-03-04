"""Agentic work completion engine with deep research, tiered builders, and 3-stage review.

Pipeline:
    Job → Route (classify tier) → Deep Research (web search, package lookup, docs)
      → Setup workspace (with RESEARCH.md) → Run builder agent → Code-simplify
      → Alignment check → Fix gaps (if any) → Code-simplify → 3x Review → Submit

Code-simplifier runs continuously: after initial build, after each alignment fix,
and before final review — ensuring clean, maintainable code at every stage.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .alignment import AlignmentMonitor, AlignmentReport
from .config import Config
from .models import Job
from .claude_cli import ClaudeCLI
from .job_router import classify, JobTier, RoutingResult
from .json_utils import extract_json
from .researcher import Researcher, ResearchBrief
from .sanitize import sanitize_text

log = logging.getLogger(__name__)

# --- Directory where templates and knowledge live ---
_PKG_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = _PKG_ROOT / "templates"
KNOWLEDGE_DIR = _PKG_ROOT / "knowledge"

# Dotfiles that should be included in deliverables
_ALLOWED_DOTFILES = {
    ".gitignore", ".env.example", ".eslintrc", ".eslintrc.js", ".eslintrc.json",
    ".prettierrc", ".prettierrc.json", ".npmrc", ".nvmrc", ".editorconfig",
    ".dockerignore", ".flake8", ".isort.cfg", ".pre-commit-config.yaml",
    ".github",
}

# Directories to skip during file collection
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "dist", "build",
    ".venv", "venv", ".tox", "coverage", ".mypy_cache",
}


# --- Review prompts ---

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


def cleanup_stale_workspaces(max_age_hours: int = 24) -> int:
    """Remove stale near_work_* temp directories.

    Called on agent startup to clean up after crashes/OOM kills.
    Returns count of directories removed.
    """
    import time
    cleaned = 0
    tmp_dir = tempfile.gettempdir()
    cutoff = time.time() - (max_age_hours * 3600)

    for pattern in ["near_work_text_*", "near_work_package_*",
                    "near_work_service_*", "near_work_system_*"]:
        for path in glob.glob(os.path.join(tmp_dir, pattern)):
            try:
                mtime = os.path.getmtime(path)
                if mtime < cutoff and os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    cleaned += 1
                    log.info(f"Cleaned stale workspace: {path}")
            except OSError:
                continue

    if cleaned:
        log.info(f"Cleaned {cleaned} stale workspace(s)")
    return cleaned


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
    alignment_reports: list[AlignmentReport] = field(default_factory=list)

    @property
    def preview(self) -> str:
        return self.content[:200] + ("..." if len(self.content) > 200 else "")

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        return {
            "job_id": self.job_id,
            "content_hash": self.content_hash,
            "tokens_used": self.tokens_used,
            "model": self.model,
            "reviews": [
                {"stage": r.stage, "score": r.score, "passed": r.passed, "feedback": r.feedback}
                for r in self.reviews
            ],
            "revisions": self.revisions,
            "tier": self.tier,
            "workspace_files": self.workspace_files,
        }


class WorkEngine:
    """Agentic work engine with tiered builders, continuous simplification, and 3-stage review.

    Pipeline:
        Route → Research → Setup workspace → Build → Simplify → Align check
          → Fix gaps (if any) → Simplify → 3x Review → Submit

    Code-simplifier runs after every major code-producing step.
    """

    MAX_REVISIONS_PER_STAGE = 2

    def __init__(self, config: Config):
        self.config = config
        self.claude = ClaudeCLI(model=config.model, max_tokens=config.max_tokens)
        self.researcher = Researcher(self.claude)
        self.alignment = AlignmentMonitor(self.claude)

    # --- Workspace setup ---

    def _setup_workspace(
        self, job: Job, routing: RoutingResult,
        research: ResearchBrief | None = None,
    ) -> str:
        """Create a temp workspace with job description, template, knowledge, and research."""
        workspace = tempfile.mkdtemp(prefix=f"near_work_{routing.tier.value}_")
        log.info(f"Created workspace: {workspace} (tier={routing.tier.value})")

        # Write job description (sanitized to prevent prompt injection)
        safe_desc = sanitize_text(job.description, max_length=10000)
        job_md = (
            f"# Job Requirements\n\n"
            f"**Title:** {sanitize_text(job.title, max_length=500)}\n"
            f"**Budget:** {job.budget_near} NEAR\n"
            f"**Tags:** {', '.join(job.tags) if job.tags else 'none'}\n\n"
            f"## Description\n\n{safe_desc}\n"
        )
        Path(workspace, "JOB.md").write_text(job_md)

        # Write research brief if available
        if research and research.content:
            research_md = research.content
            if research.sources:
                research_md += "\n\n## Sources\n\n"
                for src in research.sources[:20]:
                    research_md += f"- {src}\n"
            Path(workspace, "RESEARCH.md").write_text(research_md)
            log.info(f"Research brief written: {len(research.content)} chars, {len(research.sources)} sources")

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
                log.info(f"Loaded template: {routing.template}")

        # Copy NEAR knowledge base
        near_ref = KNOWLEDGE_DIR / "near-reference.md"
        if near_ref.exists():
            shutil.copy2(near_ref, Path(workspace, "NEAR-REFERENCE.md"))

        return workspace

    def _should_include_file(self, rel_path: str) -> bool:
        """Check if a file should be included in the deliverable."""
        basename = os.path.basename(rel_path)
        dirname = rel_path.split(os.sep)[0] if os.sep in rel_path else ""

        # Always include allowed dotfiles
        if basename in _ALLOWED_DOTFILES or dirname in _ALLOWED_DOTFILES:
            return True

        # Exclude other dotfiles
        if rel_path.startswith("."):
            return False

        return True

    def _collect_deliverable(self, workspace: str, routing: RoutingResult) -> tuple[str, list[str]]:
        """Collect the deliverable content from the workspace.

        For text jobs: return content of DELIVERABLE.md
        For code jobs: return concatenated file listing + key file contents
        """
        files = []
        for root, dirs, filenames in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in filenames:
                rel = os.path.relpath(os.path.join(root, f), workspace)
                if self._should_include_file(rel):
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
                     "src/__init__.py", "Dockerfile", "manifest.json",
                     ".eslintrc.json", ".prettierrc", ".github/workflows/ci.yml"]
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
            f"Read JOB.md for the job description. "
            f"Read REQUIREMENTS.md for the specific requirements checklist — "
            f"you MUST address every item marked as 'must'. "
            f"You have NEAR-REFERENCE.md for protocol knowledge. "
        )
        # Reference research brief if it exists
        research_path = os.path.join(workspace, "RESEARCH.md")
        if os.path.exists(research_path):
            prompt += (
                "IMPORTANT: Read RESEARCH.md — it contains deep research on the "
                "technologies, APIs, packages, and documentation needed for this job. "
                "Use the specific APIs, code patterns, and package versions documented there. "
            )
        if routing.template:
            prompt += f"A '{routing.template}' template is pre-loaded — build on top of it. "
        prompt += "Build the complete deliverable. Run tests if applicable."

        tier_timeout = self.config.tiers.timeout_for(routing.tier.value)
        tier_model = self.config.tiers.model_for(routing.tier.value, self.config.model)

        log.info(
            f"Running builder agent={routing.agent} tier={routing.tier.value} "
            f"timeout={tier_timeout}s model={tier_model}"
        )

        try:
            self.claude.run_agent(
                agent=routing.agent,
                prompt=prompt,
                workdir=workspace,
                timeout=tier_timeout,
                model=tier_model,
            )
        except RuntimeError as e:
            log.warning(f"Agentic builder failed ({e}), falling back to prompt mode")
            return self._fallback_generate(job, workspace, routing)

        content, files = self._collect_deliverable(workspace, routing)
        log.info(f"Builder produced {len(files)} files, {len(content)} chars")
        return content

    def _fallback_generate(self, job: Job, workspace: str, routing: RoutingResult) -> str:
        """Fallback to prompt-mode text generation if agentic mode fails."""
        safe_desc = sanitize_text(job.description, max_length=8000)
        system = (
            "You are an autonomous agent completing freelance work on market.near.ai. "
            "Produce high-quality work that exceeds expectations. "
            "Use proper markdown formatting. Be thorough and comprehensive. "
            "Output ONLY the deliverable content."
        )
        user = (
            f"Complete this job:\n\n"
            f"Title: {sanitize_text(job.title, max_length=500)}\n"
            f"Budget: {job.budget_near} NEAR\n"
            f"Tags: {', '.join(job.tags) if job.tags else 'none'}\n\n"
            f"Description:\n{safe_desc}\n\n"
            f"Produce the complete deliverable now."
        )
        content = self.claude.create_message(system=system, user=user, max_tokens=self.config.max_tokens)

        Path(workspace, "DELIVERABLE.md").write_text(content)
        log.info(f"Fallback generated {len(content)} chars")
        return content

    # --- Code simplification ---

    def _simplify(self, job: Job, workspace: str, routing: RoutingResult) -> None:
        """Run code-simplifier agent on workspace files."""
        if routing.tier == JobTier.TEXT:
            target = "DELIVERABLE.md"
        else:
            target = "all source files"

        log.info(f"Running code-simplifier on {target}")

        try:
            self.claude.run_agent(
                agent="code-simplifier",
                prompt=f"Simplify {target} in this project. Keep all functionality intact.",
                workdir=workspace,
            )
            log.info("Code simplification complete")
        except RuntimeError as e:
            log.warning(f"Code simplification failed (non-fatal): {e}")

    # --- Review pipeline ---

    def _review_prompt(self, job: Job, deliverable: str) -> str:
        safe_desc = sanitize_text(job.description, max_length=4000)
        return (
            f"JOB TITLE: {sanitize_text(job.title, max_length=500)}\n"
            f"BUDGET: {job.budget_near} NEAR\n"
            f"REQUIREMENTS:\n{safe_desc}\n\n"
            f"---\n\n"
            f"DELIVERABLE:\n{_truncate_at_line(deliverable, 12000)}"
        )

    def _run_review(self, stage: str, system: str, job: Job, deliverable: str) -> ReviewResult:
        log.info(f"Running review stage: {stage}")
        raw_response = self.claude.create_message(
            system=system,
            user=self._review_prompt(job, deliverable),
            max_tokens=1024,
        )
        parsed = extract_json(raw_response)
        score = float(parsed.get("score", 0.5))
        passed = bool(parsed.get("pass", False)) or parsed.get("verdict") == "ship"
        feedback = parsed.get("feedback", "") or ""
        if not passed and not feedback:
            issues = parsed.get("missing", []) or parsed.get("issues", [])
            if issues:
                feedback = "; ".join(str(i) for i in issues)

        result = ReviewResult(stage=stage, score=score, passed=passed, feedback=feedback, raw=parsed)
        log.info(f"Review {stage}: score={score:.2f} passed={passed}")
        return result

    def _revise(self, job: Job, deliverable: str, feedback: str) -> str:
        safe_desc = sanitize_text(job.description, max_length=4000)
        prompt = (
            f"ORIGINAL JOB:\n{sanitize_text(job.title, max_length=500)}\n{safe_desc}\n\n"
            f"CURRENT DELIVERABLE:\n{_truncate_at_line(deliverable, 8000)}\n\n"
            f"REVIEW FEEDBACK:\n{feedback}\n\n"
            f"Revise the deliverable to address all feedback. Output the complete revised version."
        )
        log.info(f"Revising deliverable based on feedback: {feedback[:100]}...")
        return self.claude.create_message(
            system=REVISE_SYSTEM, user=prompt, max_tokens=self.config.max_tokens,
        )

    def _revise_agentic(self, job: Job, routing: RoutingResult,
                        workspace: str, feedback: str) -> str:
        """Revise using agentic mode — re-runs builder agent with feedback context."""
        safe_feedback = sanitize_text(feedback, max_length=4000)
        prompt = (
            f"Read JOB.md for original requirements. "
            f"The previous deliverable received this feedback:\n\n{safe_feedback}\n\n"
            f"Fix all issues raised. Keep everything that was good. Run tests if applicable."
        )

        tier_timeout = self.config.tiers.timeout_for(routing.tier.value)
        tier_model = self.config.tiers.model_for(routing.tier.value, self.config.model)

        log.info(f"Running agentic revision with agent={routing.agent}")

        try:
            self.claude.run_agent(
                agent=routing.agent,
                prompt=prompt,
                workdir=workspace,
                timeout=tier_timeout,
                model=tier_model,
            )
        except RuntimeError as e:
            log.warning(f"Agentic revision failed ({e}), falling back to prompt-mode revision")
            content, _ = self._collect_deliverable(workspace, routing)
            return self._revise(job, content, feedback)

        content, _ = self._collect_deliverable(workspace, routing)
        return content

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

    def _run_review_pipeline(
        self, job: Job, content: str, tier: str,
        workspace_files: list[str] | None = None,
    ) -> WorkResult:
        """Run 3-stage review pipeline and return a WorkResult."""
        reviews: list[ReviewResult] = []

        for stage_name, system_prompt in [
            ("requirements", REVIEW_1_REQUIREMENTS),
            ("quality", REVIEW_2_QUALITY),
            ("final", REVIEW_3_FINAL),
        ]:
            content, _ = self._run_stage(stage_name, system_prompt, job, content, reviews)

        total_revisions = sum(1 for r in reviews if not r.passed)
        log.info(
            f"Review pipeline done: {len(content)} chars, "
            f"{len(reviews)} reviews, {total_revisions} revisions"
        )

        return WorkResult(
            job_id=job.job_id,
            content=content,
            content_hash=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
            model=self.config.model,
            reviews=reviews,
            revisions=total_revisions,
            tier=tier,
            workspace_files=workspace_files or [],
        )

    # --- Alignment gap fix ---

    def _fix_alignment_gaps(
        self, job: Job, content: str, report: AlignmentReport,
        workspace: str, routing: RoutingResult,
    ) -> str:
        """Targeted fix for critical alignment gaps found at a checkpoint."""
        gaps_text = "\n".join(f"- {gap}" for gap in report.critical_gaps)
        suggestions_text = "\n".join(f"- {s}" for s in report.suggestions) if report.suggestions else ""

        failed_reqs = []
        for check in report.checks:
            if check.status == "fail":
                req = next((r for r in report.requirements if r.id == check.id), None)
                if req:
                    failed_reqs.append(f"- [{req.id}] {req.description}: {check.evidence}")

        feedback = (
            f"The deliverable has critical gaps that must be fixed:\n\n"
            f"## Failed Requirements\n{chr(10).join(failed_reqs)}\n\n"
            f"## Critical Gaps\n{gaps_text}\n"
        )
        if suggestions_text:
            feedback += f"\n## Suggested Fixes\n{suggestions_text}\n"

        log.info(f"Fixing {len(report.critical_gaps)} alignment gaps")

        # Use agentic revision for code tiers, prompt revision for text
        if routing.tier != JobTier.TEXT:
            return self._revise_agentic(job, routing, workspace, feedback)
        else:
            return self._revise(job, content, feedback)

    # --- Main pipeline ---

    def complete_job(self, job: Job) -> WorkResult:
        """Complete a job through the full agentic pipeline."""
        log.info(f"Starting job: {job.title[:60]} (budget={job.budget_near} NEAR)")
        alignment_reports: list[AlignmentReport] = []

        # Step 1: Route
        routing = classify(job)
        log.info(f"Routed to tier={routing.tier.value} agent={routing.agent} reason={routing.reason}")

        if self.config.tiers.is_disabled(routing.tier.value):
            raise RuntimeError(f"Tier {routing.tier.value} is disabled via config")

        # Step 2: Extract requirements checklist
        log.info("Extracting requirements checklist...")
        requirements = self.alignment.extract_requirements(job.title, job.description)
        log.info(f"Extracted {len(requirements)} requirements")

        # Step 3: Deep research
        log.info("Starting deep research phase...")
        research = self.researcher.research_job(job.title, job.description)
        log.info(
            f"Research complete: {len(research.content)} chars, "
            f"{len(research.sources)} sources, {len(research.packages_found)} packages"
        )

        # CHECKPOINT 1: Post-research alignment
        research_report = self.alignment.check_alignment(
            "post-research",
            research.content,
            context=job.description,
        )
        alignment_reports.append(research_report)
        log.info(f"Checkpoint 1: {research_report.summary()}")

        # Step 4: Setup workspace (with research brief + requirements)
        workspace = self._setup_workspace(job, routing, research=research)

        # Write requirements checklist to workspace for the builder
        req_md = "# Requirements Checklist\n\n"
        req_md += "The builder MUST address every requirement below:\n\n"
        for r in requirements:
            req_md += f"- [ ] **[{r.id}]** ({r.priority}) {r.description}\n"
        if research_report.critical_gaps:
            req_md += "\n## ⚠️ Research Gaps (need extra attention)\n\n"
            for gap in research_report.critical_gaps:
                req_md += f"- {gap}\n"
        Path(workspace, "REQUIREMENTS.md").write_text(req_md)

        try:
            # Step 5: Run builder agent
            content = self._run_builder(job, routing, workspace)

            # Step 5a: Code-simplify (post-build)
            log.info("Running code-simplifier (post-build)")
            self._simplify(job, workspace, routing)

            # Re-collect after simplification
            if routing.tier != JobTier.TEXT or not content:
                content, files = self._collect_deliverable(workspace, routing)
            else:
                deliverable_path = os.path.join(workspace, "DELIVERABLE.md")
                if os.path.exists(deliverable_path):
                    content = Path(deliverable_path).read_text()
                files = []

            # CHECKPOINT 2: Post-build alignment
            build_report = self.alignment.check_alignment(
                "post-build",
                content,
                context=job.description,
            )
            alignment_reports.append(build_report)
            log.info(f"Checkpoint 2: {build_report.summary()}")

            # If critical gaps found, fix then simplify again
            if build_report.critical_gaps:
                log.warning(
                    f"Post-build alignment found {len(build_report.critical_gaps)} critical gaps — "
                    f"running targeted fix"
                )
                content = self._fix_alignment_gaps(
                    job, content, build_report, workspace, routing,
                )

                # Code-simplify again after gap fix
                log.info("Running code-simplifier (post-fix)")
                self._simplify(job, workspace, routing)

                # Re-collect after simplification
                if routing.tier != JobTier.TEXT or not content:
                    content, files = self._collect_deliverable(workspace, routing)
                else:
                    deliverable_path = os.path.join(workspace, "DELIVERABLE.md")
                    if os.path.exists(deliverable_path):
                        content = Path(deliverable_path).read_text()
                    files = []

            # CHECKPOINT 3: Pre-submit alignment
            submit_report = self.alignment.check_alignment(
                "pre-submit",
                content,
                context=job.description,
            )
            alignment_reports.append(submit_report)
            log.info(f"Checkpoint 3: {submit_report.summary()}")

            # Write final alignment report to workspace
            final_report = "\n\n---\n\n".join(r.to_markdown() for r in alignment_reports)
            Path(workspace, "ALIGNMENT_REPORT.md").write_text(final_report)

            # Step 7: Review pipeline
            result = self._run_review_pipeline(
                job, content, routing.tier.value,
                workspace_files=files if routing.tier != JobTier.TEXT else [],
            )
            result.alignment_reports = alignment_reports
            return result
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            log.info(f"Cleaned workspace: {workspace}")

    def handle_revision(self, job: Job, original: str, feedback: str) -> WorkResult:
        """Handle a revision request using agentic mode when applicable."""
        log.info(f"Handling revision for: {job.title[:60]}")

        routing = classify(job)

        if routing.tier != JobTier.TEXT:
            # Research again for revisions — feedback may reference new requirements
            research = self.researcher.research_job(job.title, f"{job.description}\n\nRevision feedback: {feedback}")
            workspace = self._setup_workspace(job, routing, research=research)
            try:
                Path(workspace, "PREVIOUS_DELIVERABLE.md").write_text(original)
                revised = self._revise_agentic(job, routing, workspace, feedback)
                # Simplify after revision
                self._simplify(job, workspace, routing)
                return self._run_review_pipeline(job, revised, routing.tier.value)
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
        else:
            revised = self._revise(job, original, feedback)
            return self._run_review_pipeline(job, revised, routing.tier.value)

    async def complete_job_async(self, job: Job) -> WorkResult:
        return await asyncio.to_thread(self.complete_job, job)

    async def handle_revision_async(self, job: Job, original: str, feedback: str) -> WorkResult:
        return await asyncio.to_thread(self.handle_revision, job, original, feedback)
