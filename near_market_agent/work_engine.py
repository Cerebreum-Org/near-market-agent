"""LLM-powered work completion engine with 3-stage review pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

from .config import Config
from .models import Job
from .claude_cli import ClaudeCLI


# --- Prompts ---

WORK_SYSTEM = """You are an autonomous agent completing freelance work on market.near.ai.
You have been awarded a job and must produce the deliverable.

Your approach:
1. Carefully read the job requirements
2. Plan your approach
3. Produce high-quality work that exceeds expectations
4. Structure the output clearly with proper formatting

Quality standards:
- Be thorough and comprehensive
- Use proper markdown formatting
- Include citations/sources where relevant
- If code: include comments, error handling, type hints
- If research: include methodology, findings, analysis
- If writing: proper structure, engaging, factual

Output ONLY the deliverable content. No meta-commentary about the task."""

WORK_USER = """Complete this job:

Title: {title}
Budget: {budget} NEAR
Tags: {tags}

Full Description:
{description}

---

Produce the complete deliverable now. Be thorough and comprehensive. The quality of your work directly affects your reputation on this marketplace."""

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
    """Truncate text at a newline boundary to avoid splitting mid-line."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    return text[:cut] if cut != -1 else text[:max_chars]


def _extract_json(text: str) -> dict:
    """Extract JSON from text that may contain markdown or reasoning."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
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

    # Fallback — treat as failing review
    return {"score": 0.5, "pass": False, "feedback": text[:500]}


@dataclass
class ReviewResult:
    """Result from a single review step."""
    stage: str
    score: float
    passed: bool
    feedback: str
    raw: dict = field(default_factory=dict)


@dataclass
class WorkResult:
    """Result of completing a job."""
    job_id: str
    content: str
    content_hash: str
    tokens_used: int = 0
    model: str = ""
    reviews: list[ReviewResult] = field(default_factory=list)
    revisions: int = 0

    @property
    def preview(self) -> str:
        """First 200 chars of content."""
        return self.content[:200] + ("..." if len(self.content) > 200 else "")


class WorkEngine:
    """Uses Claude to complete jobs with a 3-stage review pipeline.

    Pipeline:
        Generate → Review 1 (Requirements) → Review 2 (Quality) → Review 3 (Final Gate)
                     ↓ fail                     ↓ fail                ↓ fail
                   Revise + retry             Revise + retry        Revise + retry

    Max 2 revision attempts per review stage. If all 3 pass → ship.
    """

    MAX_REVISIONS_PER_STAGE = 2

    def __init__(self, config: Config):
        self.config = config
        self.claude = ClaudeCLI(model=config.model, max_tokens=config.max_tokens)

    def _make_result(
        self,
        job_id: str,
        content: str,
        reviews: list[ReviewResult] | None = None,
        revisions: int = 0,
    ) -> WorkResult:
        """Build a WorkResult from raw text content."""
        if not content:
            raise RuntimeError(f"Empty response from Claude CLI for job {job_id}")
        return WorkResult(
            job_id=job_id,
            content=content,
            content_hash=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
            tokens_used=0,
            model=self.config.model,
            reviews=reviews or [],
            revisions=revisions,
        )

    def _job_prompt(self, job: Job) -> str:
        """Build the user prompt for a job."""
        return WORK_USER.format(
            title=job.title,
            budget=job.budget_near,
            tags=", ".join(job.tags) if job.tags else "none",
            description=job.description[:8000],
        )

    def _review_prompt(self, job: Job, deliverable: str) -> str:
        """Build context for review prompts."""
        return (
            f"JOB TITLE: {job.title}\n"
            f"BUDGET: {job.budget_near} NEAR\n"
            f"REQUIREMENTS:\n{job.description[:4000]}\n\n"
            f"---\n\n"
            f"DELIVERABLE:\n{_truncate_at_line(deliverable, 12000)}"
        )

    def _run_review(self, stage: str, system: str, job: Job, deliverable: str) -> ReviewResult:
        """Run a single review step."""
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
            # Collect any issue lists as feedback
            issues = parsed.get("missing", []) or parsed.get("issues", [])
            if issues:
                feedback = "; ".join(str(i) for i in issues)

        return ReviewResult(
            stage=stage,
            score=score,
            passed=passed,
            feedback=feedback,
            raw=parsed,
        )

    def _revise(self, job: Job, deliverable: str, feedback: str) -> str:
        """Revise a deliverable based on review feedback."""
        prompt = (
            f"ORIGINAL JOB:\n{job.title}\n{job.description[:4000]}\n\n"
            f"CURRENT DELIVERABLE:\n{_truncate_at_line(deliverable, 8000)}\n\n"
            f"REVIEW FEEDBACK:\n{feedback}\n\n"
            f"Revise the deliverable to address all feedback. Output the complete revised version."
        )
        return self.claude.create_message(
            system=REVISE_SYSTEM,
            user=prompt,
            max_tokens=self.config.max_tokens,
        )

    def _run_stage(
        self,
        stage_name: str,
        system_prompt: str,
        job: Job,
        deliverable: str,
        reviews: list[ReviewResult],
    ) -> tuple[str, bool]:
        """Run a review stage with up to MAX_REVISIONS_PER_STAGE retries.

        Returns (possibly_revised_deliverable, passed).
        """
        for attempt in range(1 + self.MAX_REVISIONS_PER_STAGE):
            review = self._run_review(stage_name, system_prompt, job, deliverable)
            reviews.append(review)

            if review.passed:
                return deliverable, True

            if attempt < self.MAX_REVISIONS_PER_STAGE:
                deliverable = self._revise(job, deliverable, review.feedback)

        # Failed after all retries
        return deliverable, False

    def complete_job(self, job: Job) -> WorkResult:
        """Complete a job through the full generate + 3-review pipeline."""
        reviews: list[ReviewResult] = []
        total_revisions = 0

        # Step 1: Generate initial deliverable
        content = self.claude.create_message(
            system=WORK_SYSTEM,
            user=self._job_prompt(job),
            max_tokens=self.config.max_tokens,
        )

        # Step 2: Review 1 — Requirements check
        content, passed_1 = self._run_stage(
            "requirements", REVIEW_1_REQUIREMENTS, job, content, reviews,
        )
        total_revisions += sum(1 for r in reviews if not r.passed)

        # Step 3: Review 2 — Quality check
        content, passed_2 = self._run_stage(
            "quality", REVIEW_2_QUALITY, job, content, reviews,
        )
        total_revisions += sum(
            1 for r in reviews if r.stage == "quality" and not r.passed
        )

        # Step 4: Review 3 — Final gate
        content, passed_3 = self._run_stage(
            "final", REVIEW_3_FINAL, job, content, reviews,
        )
        total_revisions += sum(
            1 for r in reviews if r.stage == "final" and not r.passed
        )

        return self._make_result(
            job.job_id, content, reviews=reviews, revisions=total_revisions,
        )

    def handle_revision(self, job: Job, original: str, feedback: str) -> WorkResult:
        """Handle a revision request from the requester, then re-run reviews."""
        # First, revise based on requester feedback
        revised = self._revise(job, original, feedback)

        # Then run it through all 3 reviews again
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

        return self._make_result(
            job.job_id, revised, reviews=reviews, revisions=total_revisions,
        )

    async def complete_job_async(self, job: Job) -> WorkResult:
        return await asyncio.to_thread(self.complete_job, job)

    async def handle_revision_async(self, job: Job, original: str, feedback: str) -> WorkResult:
        return await asyncio.to_thread(self.handle_revision, job, original, feedback)
