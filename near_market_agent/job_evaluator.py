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
        """Fast rule-based filter before LLM assessment. Returns skip reason or None.

        Aggressively filters to minimize expensive Claude CLI calls.
        Only jobs that genuinely need LLM evaluation should pass.
        """
        if not job.title or not job.description:
            return "Missing title or description"

        title_lower = job.title.lower()
        desc_lower = job.description.lower()
        combined = f"{title_lower} {desc_lower}"

        if job.is_expired:
            return "Job is expired"

        if job.budget_near < self.config.min_budget_near:
            return f"Budget too low ({job.budget_near} < {self.config.min_budget_near} NEAR)"

        routing = classify(job)
        if self.config.tiers.is_disabled(routing.tier.value):
            return f"Tier {routing.tier.value} is disabled"

        # --- Platform-specific jobs we can't do ---
        gpt_signals = ["custom gpt", "gpt store", "gpt -", "chatgpt plugin",
                        "openai plugin", "gpts"]
        for sig in gpt_signals:
            if sig in title_lower:
                return f"GPT Store/plugin job (matched: {sig})"

        if "autogpt" in title_lower:
            return "AutoGPT plugin job"

        if "poe -" in title_lower or "poe bot" in title_lower:
            return "Poe platform job"

        if "huggingface" in title_lower or "hugging face" in title_lower:
            return "HuggingFace platform job"

        if "perplexity" in title_lower:
            return "Perplexity platform job"

        # --- Multimedia / creative jobs ---
        multimedia_signals = [
            "create a video", "record a video", "make a video",
            "tiktok video", "youtube video", "record audio",
            "voice recording", "short video", "video demo", "video script",
            "infographic", "graphic generator", "graphics generator",
            "image generat", "logo design",
        ]
        for signal in multimedia_signals:
            if signal in combined:
                return f"Multimedia/creative job (matched: {signal})"

        # --- Physical / real-world tasks ---
        physical_signals = [
            "pick up", "deliver to", "in-person", "photograph",
            "plant a tree", "clean a car", "physical",
        ]
        for signal in physical_signals:
            if signal in combined:
                return f"Physical task (matched: {signal})"

        # --- Social media account jobs ---
        social_signals = [
            "account growth", "manage account", "post on twitter",
            "post on reddit", "reddit account", "twitter account",
            "social media manager", "community engagement",
            "developer community engagement",
            "wikipedia presence", "wikipedia edit",
            "answer bot - reddit", "answer bot - stack overflow",
            "twitter mention monitor", "mention monitor",
        ]
        for signal in social_signals:
            if signal in combined:
                return f"Social media/account job (matched: {signal})"

        # --- Low-value spam patterns ---
        if " v2" in job.title or " v3" in job.title or " v4" in job.title:
            if job.budget_near < 15:
                return f"Low-budget template job ({job.budget_near} NEAR, versioned)"

        if ("npm package" in title_lower or "pypi package" in title_lower
                or "npm package" in desc_lower):
            if job.budget_near < 10:
                return f"Low-budget package job ({job.budget_near} NEAR)"

        if "mcp server" in title_lower:
            if job.budget_near < 10:
                return f"Low-budget MCP server job ({job.budget_near} NEAR)"

        if "langchain" in title_lower:
            if job.budget_near < 10:
                return f"Low-budget LangChain job ({job.budget_near} NEAR)"

        bot_patterns = ["discord bot", "telegram bot", "slack bot",
                        "chrome extension", "vs code extension",
                        "github action", "openclaw skill"]
        for pat in bot_patterns:
            if pat in title_lower and job.budget_near < 10:
                return f"Low-budget {pat} job ({job.budget_near} NEAR)"

        if "create gpt" in title_lower or "gpt:" in title_lower:
            return "GPT creation job"

        if (job.bid_count or 0) > 10:
            return f"Too many existing bids ({job.bid_count})"

        if job.budget_near < 5:
            return f"Budget below effective minimum ({job.budget_near} NEAR)"

        if "youtube" in combined or "free course" in combined or "online course" in combined:
            return "Course/YouTube creation job"

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
