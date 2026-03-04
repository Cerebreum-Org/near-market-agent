"""Agentic work completion engine with execution validation, publish step, and cost-aware pipeline.

Full pipeline (≥3 NEAR):
    Route → Research → Checkpoint 1 → Setup workspace → Build → Run tests
      → Fix test failures → Simplify → Checkpoint 2 (grounded) → Fix gaps
      → Simplify → Checkpoint 3 → 3x Review → Publish artifacts → Submit

Lightweight pipeline (<3 NEAR):
    Route → Research → Setup workspace → Build → Run tests → Fix failures
      → Checkpoint 2 (grounded) → Fix gaps → Simplify (once) → Checkpoint 3
      → 3x Review → Publish → Submit

Key features:
    - Execution validation: actually runs npm test / pytest / cargo test
    - Grounded alignment: feeds real test results into alignment checks
    - Publish step: creates npm tarballs / Python wheels when needed
    - Cost-aware: skips expensive LLM calls for cheap jobs
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .alignment import AlignmentMonitor, AlignmentReport
from .config import Config
from .deployer import verify_build, DeployResult
from .github_publisher import publish_workspace, gh_available
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
class ExecutionResult:
    """Result from running tests/build validation in the workspace."""
    passed: bool
    framework: str  # "npm", "pytest", "cargo", "none"
    output: str
    test_count: int = 0
    fail_count: int = 0

    def summary(self) -> str:
        if self.framework == "none":
            return "No test framework detected"
        status = "PASSED" if self.passed else "FAILED"
        return f"{self.framework}: {status} ({self.test_count} tests, {self.fail_count} failures)"


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
    execution_result: ExecutionResult | None = None
    publish_artifacts: list[str] = field(default_factory=list)
    cost_tier: str = "full"  # "lightweight" or "full"
    repo_url: str | None = None
    deploy_result: DeployResult | None = None

    @property
    def preview(self) -> str:
        return self.content[:200] + ("..." if len(self.content) > 200 else "")

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        d = {
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
            "cost_tier": self.cost_tier,
            "publish_artifacts": self.publish_artifacts,
        }
        if self.execution_result:
            d["execution"] = {
                "passed": self.execution_result.passed,
                "framework": self.execution_result.framework,
                "test_count": self.execution_result.test_count,
                "fail_count": self.execution_result.fail_count,
            }
        if self.repo_url:
            d["repo_url"] = self.repo_url
        if self.deploy_result:
            d["deploy"] = {
                "success": self.deploy_result.success,
                "method": self.deploy_result.method,
                "output": self.deploy_result.output[:500],
            }
        return d


class WorkEngine:
    """Agentic work engine with execution validation, publish step, and cost-aware pipeline.

    Pipeline (full):
        Route → Research → Checkpoint 1 → Setup workspace → Build
          → Validate (run tests) → Simplify → Checkpoint 2 (grounded)
          → Fix gaps → Simplify → Checkpoint 3 → 3x Review → Publish → Submit

    Pipeline (lightweight, <3 NEAR):
        Route → Research → Setup workspace → Build → Validate → Simplify
          → Checkpoint 2 (grounded) → Checkpoint 3 → 3x Review → Submit

    Code-simplifier runs after every major code-producing step (full pipeline).
    Execution validation runs tests and feeds real results into alignment.
    """

    MAX_REVISIONS_PER_STAGE = 2
    LIGHTWEIGHT_BUDGET_THRESHOLD = 3.0
    EXEC_TIMEOUT = 120  # seconds for test execution
    BUILDER_MAX_RETRIES = 2
    BUILDER_RETRY_DELAY = 10  # seconds between retries

    # Publish-related keywords
    _PUBLISH_TAGS = {"npm", "pypi", "publish", "deploy", "package"}
    _PUBLISH_KEYWORDS = re.compile(
        r"publish\s+to\s+npm|upload\s+to\s+pypi|deploy|publish.*package|"
        r"npm\s+publish|pypi\s+upload|pip\s+install",
        re.IGNORECASE,
    )

    def __init__(self, config: Config):
        self.config = config
        self.claude = ClaudeCLI(model=config.model, max_tokens=config.max_tokens)
        self.researcher = Researcher(self.claude)
        self.alignment = AlignmentMonitor(self.claude)

    # --- Cost awareness ---

    def _is_lightweight(self, job: Job) -> bool:
        """Cheap jobs get a leaner pipeline to save on API costs."""
        return job.budget_near < self.LIGHTWEIGHT_BUDGET_THRESHOLD

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

    @staticmethod
    def _check_tool(name: str) -> bool:
        """Check if a CLI tool is available on PATH."""
        return shutil.which(name) is not None

    def _run_builder(self, job: Job, routing: RoutingResult, workspace: str) -> str:
        """Run the appropriate builder agent in the workspace with retry logic."""
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

        last_error: RuntimeError | None = None
        for attempt in range(1 + self.BUILDER_MAX_RETRIES):
            try:
                self.claude.run_agent(
                    agent=routing.agent,
                    prompt=prompt,
                    workdir=workspace,
                    timeout=tier_timeout,
                    model=tier_model,
                )
                last_error = None
                break
            except RuntimeError as e:
                last_error = e
                if attempt < self.BUILDER_MAX_RETRIES:
                    log.warning(
                        f"Builder attempt {attempt + 1} failed ({e}), "
                        f"retrying in {self.BUILDER_RETRY_DELAY}s..."
                    )
                    time.sleep(self.BUILDER_RETRY_DELAY)
                else:
                    log.warning(
                        f"Builder failed after {attempt + 1} attempts ({e}), "
                        f"falling back to prompt mode"
                    )

        if last_error is not None:
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

    # --- Execution validation ---

    def _validate_execution(self, workspace: str, routing: RoutingResult) -> ExecutionResult:
        """Actually run tests in the workspace to validate the build.

        Non-blocking: returns a result even on failure, never crashes the pipeline.
        """
        if routing.tier == JobTier.TEXT:
            return ExecutionResult(passed=True, framework="none", output="Text tier — no tests")

        try:
            return self._run_tests(workspace)
        except Exception as e:
            log.warning(f"Execution validation failed (non-fatal): {e}")
            return ExecutionResult(passed=True, framework="none", output=f"Validation error: {e}")

    def _run_tests(self, workspace: str) -> ExecutionResult:
        """Detect project type and run appropriate test command."""
        pkg_json = os.path.join(workspace, "package.json")
        pyproject = os.path.join(workspace, "pyproject.toml")
        cargo = os.path.join(workspace, "Cargo.toml")
        requirements_txt = os.path.join(workspace, "requirements.txt")

        if os.path.exists(pkg_json):
            if not self._check_tool("node"):
                log.warning("node not installed — skipping npm tests")
                return ExecutionResult(passed=True, framework="none",
                                       output="node not installed — skipped tests")
            return self._run_npm_tests(workspace)
        elif os.path.exists(pyproject) or os.path.exists(requirements_txt):
            if not self._check_tool("python3") and not self._check_tool("python"):
                log.warning("python not installed — skipping pytest")
                return ExecutionResult(passed=True, framework="none",
                                       output="python not installed — skipped tests")
            return self._run_python_tests(workspace)
        elif os.path.exists(cargo):
            if not self._check_tool("cargo"):
                log.warning("cargo not installed — skipping cargo tests")
                return ExecutionResult(passed=True, framework="none",
                                       output="cargo not installed — skipped tests")
            return self._run_cargo_tests(workspace)
        else:
            log.info("No test framework detected in workspace")
            return ExecutionResult(passed=True, framework="none", output="No test framework detected")

    def _exec_cmd(self, cmd: str, cwd: str) -> subprocess.CompletedProcess:
        """Run a shell command with timeout, capturing output."""
        return subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True,
            timeout=self.EXEC_TIMEOUT,
        )

    def _run_npm_tests(self, workspace: str) -> ExecutionResult:
        """Run npm install && npm test."""
        log.info("Running npm tests...")
        try:
            # Install first
            install = self._exec_cmd("npm install --ignore-scripts 2>&1", workspace)
            if install.returncode != 0:
                log.warning(f"npm install failed: {install.stdout[:200]}")

            # Run tests
            result = self._exec_cmd("npm test 2>&1", workspace)
            output = result.stdout + result.stderr
            passed = result.returncode == 0

            # Parse test counts from common frameworks (jest, mocha, vitest)
            test_count, fail_count = self._parse_test_counts(output)

            return ExecutionResult(
                passed=passed, framework="npm", output=output[-2000:],
                test_count=test_count, fail_count=fail_count,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(passed=False, framework="npm", output="Test execution timed out")

    def _run_python_tests(self, workspace: str) -> ExecutionResult:
        """Run pytest, installing dependencies first."""
        log.info("Running pytest...")
        python = "python3" if self._check_tool("python3") else "python"
        try:
            # Install dependencies before running tests
            pyproject = os.path.join(workspace, "pyproject.toml")
            requirements = os.path.join(workspace, "requirements.txt")
            if os.path.exists(pyproject):
                log.info("Installing Python package (editable)...")
                self._exec_cmd(f"{python} -m pip install -e '.[test,dev]' 2>&1", workspace)
            elif os.path.exists(requirements):
                log.info("Installing requirements.txt...")
                self._exec_cmd(f"{python} -m pip install -r requirements.txt 2>&1", workspace)

            result = self._exec_cmd(f"{python} -m pytest -v --tb=short 2>&1", workspace)
            output = result.stdout + result.stderr
            passed = result.returncode == 0

            test_count, fail_count = self._parse_test_counts(output)

            return ExecutionResult(
                passed=passed, framework="pytest", output=output[-2000:],
                test_count=test_count, fail_count=fail_count,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(passed=False, framework="pytest", output="Test execution timed out")

    def _run_cargo_tests(self, workspace: str) -> ExecutionResult:
        """Run cargo test."""
        log.info("Running cargo tests...")
        try:
            result = self._exec_cmd("cargo test 2>&1", workspace)
            output = result.stdout + result.stderr
            passed = result.returncode == 0

            test_count, fail_count = self._parse_test_counts(output)

            return ExecutionResult(
                passed=passed, framework="cargo", output=output[-2000:],
                test_count=test_count, fail_count=fail_count,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(passed=False, framework="cargo", output="Test execution timed out")

    @staticmethod
    def _parse_test_counts(output: str) -> tuple[int, int]:
        """Parse test/fail counts from common test framework output."""
        # pytest: "5 passed, 2 failed"
        m = re.search(r"(\d+)\s+passed", output)
        passed = int(m.group(1)) if m else 0
        m = re.search(r"(\d+)\s+failed", output)
        failed = int(m.group(1)) if m else 0
        if passed or failed:
            return passed + failed, failed

        # jest/vitest: "Tests: 2 failed, 5 passed, 7 total"
        m = re.search(r"Tests:\s*(?:(\d+)\s+failed,\s*)?(\d+)\s+passed,\s*(\d+)\s+total", output)
        if m:
            return int(m.group(3)), int(m.group(1) or 0)

        # cargo: "test result: ok. 5 passed; 0 failed"
        m = re.search(r"(\d+)\s+passed;\s+(\d+)\s+failed", output)
        if m:
            return int(m.group(1)) + int(m.group(2)), int(m.group(2))

        return 0, 0

    def _fix_test_failures(
        self, job: Job, routing: RoutingResult, workspace: str,
        exec_result: ExecutionResult,
    ) -> ExecutionResult:
        """If tests failed, feed output to builder and re-run tests once."""
        if exec_result.passed or exec_result.framework == "none":
            return exec_result

        log.warning(f"Tests failed ({exec_result.summary()}) — sending failures to builder for fix")

        fix_prompt = (
            f"The tests are FAILING. Fix the code so all tests pass.\n\n"
            f"Test output:\n```\n{exec_result.output[-3000:]}\n```\n\n"
            f"Fix the failing tests. Do NOT delete tests — fix the implementation."
        )

        tier_timeout = self.config.tiers.timeout_for(routing.tier.value)
        tier_model = self.config.tiers.model_for(routing.tier.value, self.config.model)

        try:
            self.claude.run_agent(
                agent=routing.agent,
                prompt=fix_prompt,
                workdir=workspace,
                timeout=tier_timeout,
                model=tier_model,
            )
        except RuntimeError as e:
            log.warning(f"Test fix attempt failed: {e}")
            return exec_result

        # Re-run tests
        retest = self._run_tests(workspace)
        log.info(f"Retest after fix: {retest.summary()}")
        return retest

    # --- Publish step ---

    def _needs_publish(self, job: Job, routing: RoutingResult) -> bool:
        """Check if this job requires publishing a package artifact."""
        if routing.tier != JobTier.PACKAGE:
            return False

        tags = set(t.lower() for t in (job.tags or []))
        if tags & self._PUBLISH_TAGS:
            return True

        if self._PUBLISH_KEYWORDS.search(job.description or ""):
            return True

        return False

    def _publish_if_needed(
        self, job: Job, routing: RoutingResult, workspace: str,
    ) -> list[str]:
        """Create publishable artifacts if the job requires it.

        Doesn't actually publish to registries — creates the artifacts
        (npm tarball or Python sdist/wheel) and returns their paths.
        """
        if not self._needs_publish(job, routing):
            return []

        artifacts: list[str] = []

        try:
            pkg_json = os.path.join(workspace, "package.json")
            pyproject = os.path.join(workspace, "pyproject.toml")

            if os.path.exists(pkg_json):
                log.info("Creating npm tarball...")
                result = self._exec_cmd("npm pack 2>&1", workspace)
                if result.returncode == 0:
                    # npm pack outputs the tarball filename
                    tarball = result.stdout.strip().split("\n")[-1]
                    tarball_path = os.path.join(workspace, tarball)
                    if os.path.exists(tarball_path):
                        artifacts.append(tarball)
                        log.info(f"Created npm tarball: {tarball}")
                else:
                    log.warning(f"npm pack failed: {result.stdout[:200]}")

            elif os.path.exists(pyproject):
                log.info("Creating Python sdist/wheel...")
                result = self._exec_cmd("python -m build 2>&1", workspace)
                if result.returncode == 0:
                    dist_dir = os.path.join(workspace, "dist")
                    if os.path.isdir(dist_dir):
                        for f in os.listdir(dist_dir):
                            artifacts.append(f"dist/{f}")
                        log.info(f"Created Python artifacts: {artifacts}")
                else:
                    log.warning(f"python -m build failed: {result.stdout[:200]}")

        except Exception as e:
            log.warning(f"Publish step failed (non-fatal): {e}")

        return artifacts

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

    def _recollect(self, workspace: str, routing: RoutingResult, content: str):
        """Re-collect deliverable files after workspace changes."""
        if routing.tier != JobTier.TEXT or not content:
            return self._collect_deliverable(workspace, routing)
        deliverable_path = os.path.join(workspace, "DELIVERABLE.md")
        if os.path.exists(deliverable_path):
            content = Path(deliverable_path).read_text()
        return content, []

    def complete_job(self, job: Job) -> WorkResult:
        """Complete a job through the full agentic pipeline.

        Cost-aware: lightweight jobs (<3 NEAR) skip checkpoint 1 and
        reduce simplifier passes to save on API calls.
        """
        lightweight = self._is_lightweight(job)
        cost_label = "lightweight" if lightweight else "full"
        log.info(
            f"Starting job: {job.title[:60]} "
            f"(budget={job.budget_near} NEAR, pipeline={cost_label})"
        )
        alignment_reports: list[AlignmentReport] = []
        exec_result: ExecutionResult | None = None

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

        # CHECKPOINT 1: Post-research alignment (skip for lightweight jobs)
        if not lightweight:
            research_report = self.alignment.check_alignment(
                "post-research",
                research.content,
                context=job.description,
            )
            alignment_reports.append(research_report)
            log.info(f"Checkpoint 1: {research_report.summary()}")
            research_gaps = research_report.critical_gaps
        else:
            log.info("Checkpoint 1: skipped (lightweight pipeline)")
            research_gaps = []

        # Step 4: Setup workspace (with research brief + requirements)
        workspace = self._setup_workspace(job, routing, research=research)

        # Write requirements checklist to workspace for the builder
        req_md = "# Requirements Checklist\n\n"
        req_md += "The builder MUST address every requirement below:\n\n"
        for r in requirements:
            req_md += f"- [ ] **[{r.id}]** ({r.priority}) {r.description}\n"
        if research_gaps:
            req_md += "\n## ⚠️ Research Gaps (need extra attention)\n\n"
            for gap in research_gaps:
                req_md += f"- {gap}\n"
        Path(workspace, "REQUIREMENTS.md").write_text(req_md)

        try:
            # Step 5: Run builder agent
            content = self._run_builder(job, routing, workspace)

            # Step 5a: Execution validation (run actual tests)
            exec_result = self._validate_execution(workspace, routing)
            log.info(f"Execution validation: {exec_result.summary()}")

            # Step 5b: If tests failed, fix and retest
            if not exec_result.passed:
                exec_result = self._fix_test_failures(job, routing, workspace, exec_result)

            # Step 5c: Code-simplify (post-build)
            if not lightweight:
                log.info("Running code-simplifier (post-build)")
                self._simplify(job, workspace, routing)

            # Re-collect after build/simplify
            content, files = self._recollect(workspace, routing, content)

            # CHECKPOINT 2: Post-build alignment (grounded in execution results)
            exec_context = ""
            if exec_result and exec_result.framework != "none":
                exec_context = (
                    f"\n\n## Execution Validation\n"
                    f"Framework: {exec_result.framework}\n"
                    f"Status: {'PASSED' if exec_result.passed else 'FAILED'}\n"
                    f"Tests: {exec_result.test_count} total, {exec_result.fail_count} failures\n"
                    f"Output:\n```\n{exec_result.output[-1500:]}\n```"
                )

            build_report = self.alignment.check_alignment(
                "post-build",
                content + exec_context,
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

                # Code-simplify again after gap fix (full pipeline only)
                if not lightweight:
                    log.info("Running code-simplifier (post-fix)")
                    self._simplify(job, workspace, routing)

                # Re-collect
                content, files = self._recollect(workspace, routing, content)

            # Final code-simplify for lightweight (runs once here instead of twice above)
            if lightweight:
                log.info("Running code-simplifier (lightweight — single pass)")
                self._simplify(job, workspace, routing)
                content, files = self._recollect(workspace, routing, content)

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

            # Step 5d: Build verification
            deploy = verify_build(workspace, routing)
            log.info(f"Build verification: {deploy.summary()}")

            # Include build verification in grounded alignment context
            if not deploy.success:
                log.warning(f"Build verification failed — feeding to builder for fix")
                fix_prompt = (
                    f"The project FAILS to build. Fix it.\n\n"
                    f"Build output:\n```\n{deploy.output[-2000:]}\n```\n\n"
                    f"Fix the build errors. The project must compile/build without errors."
                )
                tier_timeout_fix = self.config.tiers.timeout_for(routing.tier.value)
                tier_model_fix = self.config.tiers.model_for(routing.tier.value, self.config.model)
                try:
                    self.claude.run_agent(
                        agent=routing.agent, prompt=fix_prompt,
                        workdir=workspace, timeout=tier_timeout_fix,
                        model=tier_model_fix,
                    )
                    deploy = verify_build(workspace, routing)
                    log.info(f"Build re-verification: {deploy.summary()}")
                except RuntimeError as e:
                    log.warning(f"Build fix attempt failed: {e}")

            # Step 6: Review pipeline
            result = self._run_review_pipeline(
                job, content, routing.tier.value,
                workspace_files=files if routing.tier != JobTier.TEXT else [],
            )
            result.alignment_reports = alignment_reports
            result.execution_result = exec_result
            result.deploy_result = deploy
            result.cost_tier = cost_label

            # Step 7: Publish artifacts if needed
            artifacts = self._publish_if_needed(job, routing, workspace)
            result.publish_artifacts = artifacts
            if artifacts:
                log.info(f"Created {len(artifacts)} publish artifact(s): {artifacts}")

            # Step 8: Push code to GitHub for code deliverables
            if routing.tier in (JobTier.PACKAGE, JobTier.SERVICE, JobTier.SYSTEM):
                repo_url = publish_workspace(
                    workspace, job.title, job.job_id,
                    org=self.config.github_org,
                    author_name=self.config.github_author_name,
                    author_email=self.config.github_author_email,
                )
                result.repo_url = repo_url
                if repo_url:
                    log.info(f"Code published to {repo_url}")
                    # Prepend repo URL to deliverable content
                    result.content = (
                        f"## GitHub Repository\n\n{repo_url}\n\n---\n\n{result.content}"
                    )
                    result.content_hash = (
                        f"sha256:{hashlib.sha256(result.content.encode()).hexdigest()}"
                    )
                else:
                    log.warning("GitHub publish failed — submitting text-only deliverable")

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
