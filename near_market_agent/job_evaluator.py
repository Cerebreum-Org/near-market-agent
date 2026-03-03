"""LLM-powered job evaluation and bid generation."""

from __future__ import annotations

import asyncio
import json

from .config import Config
from .models import Job, JobEvaluation
from .claude_cli import ClaudeCLI


EVAL_SYSTEM = """You are an autonomous agent evaluating freelance jobs on market.near.ai.
You must decide which jobs to bid on based on your capabilities and the job requirements.

Your capabilities:
{capabilities}

Jobs you should SKIP (score 0):
- Physical tasks (delivery, photography, in-person anything)
- Video/image/audio creation (you can't generate multimedia)
- Jobs requiring real social media accounts you don't have
- Scam-looking or nonsensical jobs
- Jobs that require specific credentials or licenses you don't have

Evaluation criteria:
- Can you actually complete this? (capability match)
- Is the budget reasonable for the effort? (value)
- How many bids already? (competition — fewer is better)
- Is the deadline achievable? (time)
- Is the requester reputable? (trust)

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


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from text that may contain surrounding prose.

    Tries in order:
    1. Direct parse (pure JSON)
    2. Markdown fenced block (```json ... ```)
    3. First { ... } block in the text
    """
    text = text.strip()

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown fences
    if "```" in text:
        parts = text.split("```")
        for part in parts[1::2]:  # odd-indexed parts are inside fences
            content = part.strip()
            if content.startswith("json"):
                content = content[4:].strip()
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue

    # 3. Find first { ... } block (greedy from first { to last })
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


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

    def evaluate_job(self, job: Job) -> JobEvaluation:
        """Assess a single job and return scoring + proposal."""
        preflight = self._preflight_filter(job)
        if preflight:
            return _skip_result(job.job_id, preflight)

        system = EVAL_SYSTEM.format(capabilities=self.config.capabilities.description)
        user = EVAL_USER.format(
            title=job.title,
            budget=job.budget_near,
            bid_count=job.bid_count or 0,
            tags=", ".join(job.tags) if job.tags else "none",
            expires=str(job.expires_at) if job.expires_at else "none",
            job_type=job.job_type.value,
            description=job.description[:3000],
        )

        try:
            text = self.claude.create_message(system=system, user=user, max_tokens=1024).strip()
        except RuntimeError as e:
            return _skip_result(job.job_id, f"Claude CLI error: {e}")

        if not text:
            return _skip_result(job.job_id, "Empty LLM response")

        # Extract JSON from response — Claude CLI may include reasoning before/after JSON
        data = _extract_json(text)
        if data is None:
            return _skip_result(job.job_id, f"Failed to parse LLM response: {text[:200]}")

        # Clamp score to [0, 1]
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
        """Fast rule-based filter before LLM assessment. Returns skip reason or None."""
        if not job.title or not job.description:
            return "Missing title or description"

        title_lower = job.title.lower()
        desc_lower = job.description.lower()

        if job.is_expired:
            return "Job is expired"

        if job.budget_near < self.config.min_budget_near:
            return f"Budget too low ({job.budget_near} < {self.config.min_budget_near} NEAR)"

        multimedia_signals = [
            "create a video", "record a video", "make a video",
            "tiktok video", "youtube video", "record audio",
            "voice recording", "short video", "video demo", "video script",
        ]
        for signal in multimedia_signals:
            if signal in title_lower or signal in desc_lower:
                return f"Multimedia creation job (matched: {signal})"

        physical_signals = [
            "pick up", "deliver to", "in-person", "photograph",
            "plant a tree", "clean a car",
        ]
        for signal in physical_signals:
            if signal in title_lower or signal in desc_lower:
                return f"Physical task (matched: {signal})"

        if "account growth" in title_lower or "manage account" in title_lower:
            return "Requires social media account access"

        if "warhead" in title_lower or "nuclear warhead" in desc_lower:
            return "Obviously harmful/troll job"

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
        """Assess jobs concurrently with a semaphore to avoid rate limits."""
        sem = asyncio.Semaphore(max_concurrent)

        async def _assess(job: Job) -> JobEvaluation:
            # Preflight runs instantly — skip semaphore for fast rejections
            preflight = self._preflight_filter(job)
            if preflight:
                return _skip_result(job.job_id, preflight)
            async with sem:
                try:
                    return await asyncio.to_thread(self.evaluate_job, job)
                except Exception as e:
                    return _skip_result(job.job_id, f"Assessment error: {e}")

        return list(await asyncio.gather(*[_assess(job) for job in jobs]))
