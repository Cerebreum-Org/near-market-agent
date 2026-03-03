"""LLM-powered job evaluation and bid generation."""

from __future__ import annotations

import asyncio
import json
import anthropic

from .config import Config
from .models import Job, JobEvaluation
from . import extract_llm_text


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


class JobEvaluator:
    """Uses Claude to evaluate jobs and generate bid proposals."""

    def __init__(self, config: Config):
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def evaluate_job(self, job: Job) -> JobEvaluation:
        """Evaluate a single job and return scoring + proposal."""
        # Quick pre-filter before spending tokens
        preflight = self._preflight_filter(job)
        if preflight:
            return JobEvaluation(
                job_id=job.job_id,
                score=0.0,
                should_bid=False,
                reasoning=preflight,
                category="skip",
            )

        system = EVAL_SYSTEM.format(
            capabilities=self.config.capabilities.description
        )
        user = EVAL_USER.format(
            title=job.title,
            budget=job.budget_near,
            bid_count=job.bid_count or 0,
            tags=", ".join(job.tags) if job.tags else "none",
            expires=str(job.expires_at) if job.expires_at else "none",
            job_type=job.job_type.value,
            description=job.description[:3000],
        )

        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        text = extract_llm_text(response).strip()
        if not text:
            return JobEvaluation(
                job_id=job.job_id,
                score=0.0,
                should_bid=False,
                reasoning="Empty LLM response",
                category="skip",
            )
        # Parse JSON, handling potential markdown fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return JobEvaluation(
                job_id=job.job_id,
                score=0.0,
                should_bid=False,
                reasoning=f"Failed to parse LLM response: {text[:200]}",
                category="skip",
            )

        # Clamp score to valid range
        try:
            raw_score = float(data.get("score", 0) or 0)
        except (TypeError, ValueError):
            raw_score = 0.0
        score = max(0.0, min(1.0, raw_score))

        # Validate suggested amounts
        suggested_bid = data.get("suggested_bid_amount")
        if suggested_bid is not None:
            try:
                suggested_bid = float(suggested_bid)
                if suggested_bid <= 0:
                    suggested_bid = None
            except (TypeError, ValueError):
                suggested_bid = None

        suggested_eta = data.get("suggested_eta_hours")
        if suggested_eta is not None:
            try:
                suggested_eta = int(suggested_eta)
                if suggested_eta <= 0:
                    suggested_eta = None
            except (TypeError, ValueError):
                suggested_eta = None

        return JobEvaluation(
            job_id=job.job_id,
            score=score,
            should_bid=bool(data.get("should_bid", False)),
            reasoning=str(data.get("reasoning") or ""),
            suggested_bid_amount=suggested_bid,
            suggested_eta_hours=suggested_eta,
            proposal_draft=str(data.get("proposal_draft") or ""),
            category=str(data.get("category") or "skip"),
        )

    def _preflight_filter(self, job: Job) -> str | None:
        """Fast rule-based filter before LLM eval. Returns skip reason or None."""
        # Guard against missing/empty fields
        if not job.title or not job.description:
            return "Missing title or description"

        title_lower = job.title.lower()
        desc_lower = job.description.lower()

        # Already expired
        if job.is_expired:
            return "Job is expired"

        # Budget too low
        if job.budget_near < self.config.min_budget_near:
            return f"Budget too low ({job.budget_near} < {self.config.min_budget_near} NEAR)"

        # Skip video/image/audio creation (plain string match, not regex)
        multimedia_signals = ["create a video", "record a video", "make a video",
                            "tiktok video", "youtube video", "record audio",
                            "voice recording", "short video", "video demo",
                            "video script"]
        for signal in multimedia_signals:
            if signal in title_lower or signal in desc_lower:
                return f"Multimedia creation job (matched: {signal})"

        # Skip physical tasks
        physical_signals = ["pick up", "deliver to", "in-person", "photograph",
                          "plant a tree", "clean a car"]
        for signal in physical_signals:
            if signal in title_lower or signal in desc_lower:
                return f"Physical task (matched: {signal})"

        # Skip social media account management
        if "account growth" in title_lower or "manage account" in title_lower:
            return "Requires social media account access"

        # Nuclear warhead guy and obvious trolls
        if "nuclear" in title_lower or "warhead" in title_lower:
            return "Obviously harmful/troll job"

        return None

    def batch_evaluate(self, jobs: list[Job]) -> list[JobEvaluation]:
        """Evaluate multiple jobs. Uses preflight to minimize LLM calls."""
        results = []
        for job in jobs:
            eval_result = self.evaluate_job(job)
            results.append(eval_result)
        return results

    async def evaluate_job_async(self, job: Job) -> JobEvaluation:
        """Run job evaluation off the event loop."""
        return await asyncio.to_thread(self.evaluate_job, job)

    async def batch_evaluate_async(
        self, jobs: list[Job], max_concurrent: int = 5
    ) -> list[JobEvaluation]:
        """Evaluate jobs concurrently with a semaphore to avoid rate limits."""
        sem = asyncio.Semaphore(max_concurrent)

        async def _eval(job: Job) -> JobEvaluation:
            # Preflight runs instantly — no semaphore needed
            preflight = self._preflight_filter(job)
            if preflight:
                return JobEvaluation(
                    job_id=job.job_id,
                    score=0.0,
                    should_bid=False,
                    reasoning=preflight,
                    category="skip",
                )
            async with sem:
                try:
                    # evaluate_job will re-run preflight (harmless but instant),
                    # then do the LLM call
                    return await asyncio.to_thread(self.evaluate_job, job)
                except Exception as e:
                    # Don't let one failed evaluation crash the whole batch
                    return JobEvaluation(
                        job_id=job.job_id,
                        score=0.0,
                        should_bid=False,
                        reasoning=f"Evaluation error: {e}",
                        category="skip",
                    )

        tasks = [_eval(job) for job in jobs]
        return list(await asyncio.gather(*tasks))

    # Keep static method as alias for backward compat with tests
    _extract_text = staticmethod(extract_llm_text)
