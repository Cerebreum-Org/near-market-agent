"""LLM-powered work completion engine."""

from __future__ import annotations

import asyncio
import hashlib
import anthropic

from .config import Config
from .models import Job


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

Produce the complete deliverable now. Be thorough."""


class WorkEngine:
    """Uses Claude to complete jobs and produce deliverables."""

    def __init__(self, config: Config):
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def complete_job(self, job: Job) -> WorkResult:
        """Complete a job and return the deliverable content."""
        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=WORK_SYSTEM,
            messages=[{
                "role": "user",
                "content": WORK_USER.format(
                    title=job.title,
                    budget=job.budget_near,
                    tags=", ".join(job.tags) if job.tags else "none",
                    description=job.description[:8000],
                ),
            }],
        )

        content = self._extract_text(response)
        if not content:
            raise RuntimeError(f"Empty response from LLM for job {job.job_id}")
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        return WorkResult(
            job_id=job.job_id,
            content=content,
            content_hash=f"sha256:{content_hash}",
            tokens_used=response.usage.input_tokens + response.usage.output_tokens,
            model=self.config.model,
        )

    def handle_revision(self, job: Job, original: str, feedback: str) -> WorkResult:
        """Handle a revision request from the requester."""
        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=WORK_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": WORK_USER.format(
                        title=job.title,
                        budget=job.budget_near,
                        tags=", ".join(job.tags) if job.tags else "none",
                        description=job.description[:6000],
                    ),
                },
                {
                    "role": "assistant",
                    "content": original[:4000],
                },
                {
                    "role": "user",
                    "content": f"The requester has requested changes:\n\n{feedback}\n\nPlease revise the deliverable accordingly.",
                },
            ],
        )

        content = self._extract_text(response)
        if not content:
            raise RuntimeError(f"Empty revision response from LLM for job {job.job_id}")
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        return WorkResult(
            job_id=job.job_id,
            content=content,
            content_hash=f"sha256:{content_hash}",
            tokens_used=response.usage.input_tokens + response.usage.output_tokens,
            model=self.config.model,
        )

    async def complete_job_async(self, job: Job) -> WorkResult:
        """Run job completion off the event loop."""
        return await asyncio.to_thread(self.complete_job, job)

    async def handle_revision_async(self, job: Job, original: str, feedback: str) -> WorkResult:
        """Run revision handling off the event loop."""
        return await asyncio.to_thread(self.handle_revision, job, original, feedback)

    @staticmethod
    def _extract_text(response: object) -> str:
        blocks = getattr(response, "content", None)
        if not isinstance(blocks, list):
            return ""
        parts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts).strip()


class WorkResult:
    """Result of completing a job."""

    def __init__(
        self,
        job_id: str,
        content: str,
        content_hash: str,
        tokens_used: int = 0,
        model: str = "",
    ):
        self.job_id = job_id
        self.content = content
        self.content_hash = content_hash
        self.tokens_used = tokens_used
        self.model = model

    @property
    def preview(self) -> str:
        """First 200 chars of content."""
        return self.content[:200] + ("..." if len(self.content) > 200 else "")
