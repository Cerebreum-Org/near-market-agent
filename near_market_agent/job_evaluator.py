"""LLM-powered job evaluation and bid generation."""

from __future__ import annotations

import asyncio

from .config import Config
from .models import Job, JobEvaluation
from .claude_cli import ClaudeCLI
from .json_utils import extract_json
from .job_router import classify
from .sanitize import sanitize_job


EVAL_SYSTEM = """You are an autonomous agent evaluating freelance jobs on market.near.ai.
You are a versatile AI agent that can handle virtually ANY job. You should bid aggressively.

Your capabilities:
{capabilities}

You can handle:
- Code: Python, TypeScript, Rust, Solidity, any language
- Packages: npm, pypi, MCP servers, Chrome extensions, VS Code extensions
- Writing: technical docs, guides, blog posts, research, analysis
- Bots: Discord, Telegram, Slack, any platform
- AI/ML: LangChain, agents, embeddings, fine-tuning pipelines
- NEAR protocol: smart contracts, SDKs, integrations, DeFi
- DevOps: Docker, CI/CD, deployment configs, GitHub Actions
- Data: scraping, processing, APIs, databases
- Creative: content strategy, marketing copy, newsletters

Only skip jobs that are TRULY impossible for an AI:
- Physical tasks requiring a human body (delivery, photography in-person)
- Jobs requiring specific credentials/licenses you cannot obtain
- Obviously harmful or illegal requests

For everything else, bid. Be creative about how you'd approach it.

Evaluation criteria:
- Can you produce a useful deliverable? (even partial value counts)
- Is the budget reasonable for the effort? (value)
- How many bids already? (competition — fewer is better, but don't skip just because of competition)
- Is the deadline achievable? (time)

Respond with ONLY valid JSON (no markdown):
{{
    "score": 0.0-1.0,
    "should_bid": true/false,
    "reasoning": "brief explanation",
    "suggested_bid_amount": null or number,
    "suggested_eta_hours": null or number,
    "proposal_draft": "the bid proposal text if should_bid is true",
    "category": "research|writing|code|analysis|content|skip"
}}"""

EVAL_USER = """Evaluate this job:

Title: {title}
Budget: {budget} NEAR
Current bids: {bid_count}
Tags: {tags}
Expires: {expires}
Type: {job_type}

Description:
{description}"""


def _skip_result(job_id: str, reason: str) -> JobEvaluation:
    """Create a skip evaluation (score 0, no bid)."""
    return JobEvaluation(job_id=job_id, score=0.0, should_bid=False, reasoning=reason, category="skip")


def _positive_or_none(value, cast=float):
    """Parse a value to a positive number, returning None on failure or non-positive."""
    if value is None:
        return None
    try:
        result = cast(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


class JobEvaluator:
    """Uses Claude to assess jobs and generate bid proposals."""

    def __init__(self, config: Config):
        self.config = config
        self.claude = ClaudeCLI(model=config.model)

    def evaluate_job(self, job: Job, *, skip_preflight: bool = False) -> JobEvaluation:
        """Assess a single job and return scoring + proposal.

        Args:
            job: The job to evaluate.
            skip_preflight: If True, skip preflight filter (already done by caller).
        """
        if not skip_preflight:
            preflight = self._preflight_filter(job)
            if preflight:
                return _skip_result(job.job_id, preflight)

        safe_title, safe_desc = sanitize_job(job.title, job.description)

        system = EVAL_SYSTEM.format(capabilities=self.config.capabilities.description)
        user = EVAL_USER.format(
            title=safe_title,
            budget=job.budget_near,
            bid_count=job.bid_count or 0,
            tags=", ".join(job.tags) if job.tags else "none",
            expires=str(job.expires_at) if job.expires_at else "none",
            job_type=job.job_type.value,
            description=safe_desc[:3000],
        )

        try:
            text = self.claude.create_message(system=system, user=user, max_tokens=1024).strip()
        except RuntimeError as e:
            return _skip_result(job.job_id, f"Claude CLI error: {e}")

        if not text:
            return _skip_result(job.job_id, "Empty LLM response")

        data = extract_json(text, fallback=None)
        if data is None:
            return _skip_result(job.job_id, f"Failed to parse LLM response: {text[:200]}")

        try:
            raw_score = float(data.get("score", 0) or 0)
        except (TypeError, ValueError):
            raw_score = 0.0

        return JobEvaluation(
            job_id=job.job_id,
            score=max(0.0, min(1.0, raw_score)),
            should_bid=bool(data.get("should_bid", False)),
            reasoning=str(data.get("reasoning") or ""),
            suggested_bid_amount=_positive_or_none(data.get("suggested_bid_amount")),
            suggested_eta_hours=_positive_or_none(data.get("suggested_eta_hours"), int),
            proposal_draft=str(data.get("proposal_draft") or ""),
            category=str(data.get("category") or "skip"),
        )

    def _preflight_filter(self, job: Job) -> str | None:
        """Minimal filter — only skip jobs that are truly impossible.

        We want to bid on everything we can attempt. Let the LLM decide
        if we can handle it; don't gatekeep with keyword matching.
        """
        if not job.title or not job.description:
            return "Missing title or description"

        if job.is_expired:
            return "Job is expired"

        if job.budget_near < self.config.min_budget_near:
            return f"Budget too low ({job.budget_near} < {self.config.min_budget_near} NEAR)"

        routing = classify(job)
        if self.config.tiers.is_disabled(routing.tier.value):
            return f"Tier {routing.tier.value} is disabled"

        return None

    def batch_evaluate(self, jobs: list[Job]) -> list[JobEvaluation]:
        """Assess multiple jobs sequentially."""
        return [self.evaluate_job(job) for job in jobs]

    async def evaluate_job_async(self, job: Job) -> JobEvaluation:
        """Run job assessment off the event loop."""
        return await asyncio.to_thread(self.evaluate_job, job)

    async def batch_evaluate_async(
        self, jobs: list[Job], max_concurrent: int = 5
    ) -> list[JobEvaluation]:
        """Assess jobs concurrently with a semaphore to avoid rate limits.

        Preflight runs first (instant), then LLM evaluation runs with
        skip_preflight=True to avoid redundant filtering.
        """
        sem = asyncio.Semaphore(max_concurrent)

        async def _assess(job: Job) -> JobEvaluation:
            # Preflight runs instantly — skip semaphore for fast rejections
            preflight = self._preflight_filter(job)
            if preflight:
                return _skip_result(job.job_id, preflight)
            # LLM evaluation — skip preflight since we already ran it
            async with sem:
                try:
                    return await asyncio.to_thread(
                        self.evaluate_job, job, skip_preflight=True,
                    )
                except Exception as e:
                    return _skip_result(job.job_id, f"Assessment error: {e}")

        return list(await asyncio.gather(*[_assess(job) for job in jobs]))
