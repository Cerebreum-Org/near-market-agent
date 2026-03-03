"""LLM-powered job evaluation and bid generation."""

from __future__ import annotations

import json
import anthropic

from .config import Config
from .models import Job, JobEvaluation


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

        text = response.content[0].text.strip()
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

        return JobEvaluation(
            job_id=job.job_id,
            score=float(data.get("score", 0) or 0),
            should_bid=bool(data.get("should_bid", False)),
            reasoning=data.get("reasoning") or "",
            suggested_bid_amount=data.get("suggested_bid_amount"),
            suggested_eta_hours=data.get("suggested_eta_hours"),
            proposal_draft=data.get("proposal_draft") or "",
            category=data.get("category") or "skip",
        )

    def _preflight_filter(self, job: Job) -> str | None:
        """Fast rule-based filter before LLM eval. Returns skip reason or None."""
        title_lower = job.title.lower()
        desc_lower = job.description.lower()

        # Budget too low
        if job.budget_near < self.config.min_budget_near:
            return f"Budget too low ({job.budget_near} < {self.config.min_budget_near} NEAR)"

        # Skip video/image/audio creation
        multimedia_signals = ["create a video", "record a video", "make a video",
                            "tiktok video", "youtube video", "create.*image",
                            "design.*logo", "record audio", "voice recording"]
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
        if "account growth" in title_lower or "manage.*account" in title_lower:
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
