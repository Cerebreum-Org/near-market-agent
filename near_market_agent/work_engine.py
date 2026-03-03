"""LLM-powered work completion engine."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from .config import Config
from .models import Job
from .claude_cli import ClaudeCLI


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


def _truncate_at_line(text: str, max_chars: int) -> str:
    """Truncate text at a newline boundary to avoid splitting mid-line."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    return text[:cut] if cut != -1 else text[:max_chars]


@dataclass
class WorkResult:
    """Result of completing a job."""
    job_id: str
    content: str
    content_hash: str
    tokens_used: int = 0
    model: str = ""

    @property
    def preview(self) -> str:
        """First 200 chars of content."""
        return self.content[:200] + ("..." if len(self.content) > 200 else "")


class WorkEngine:
    """Uses Claude to complete jobs and produce deliverables."""

    def __init__(self, config: Config):
        self.config = config
        self.claude = ClaudeCLI(model=config.model, max_tokens=config.max_tokens)

    def _make_result(self, job_id: str, content: str) -> WorkResult:
        """Build a WorkResult from raw text content."""
        if not content:
            raise RuntimeError(f"Empty response from Claude CLI for job {job_id}")
        return WorkResult(
            job_id=job_id,
            content=content,
            content_hash=f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
            tokens_used=0,  # CLI doesn't report token usage
            model=self.config.model,
        )

    def _job_prompt(self, job: Job) -> str:
        """Build the user prompt for a job."""
        return WORK_USER.format(
            title=job.title,
            budget=job.budget_near,
            tags=", ".join(job.tags) if job.tags else "none",
            description=job.description[:8000],
        )

    def complete_job(self, job: Job) -> WorkResult:
        """Complete a job and return the deliverable content."""
        content = self.claude.create_message(
            system=WORK_SYSTEM,
            user=self._job_prompt(job),
            max_tokens=self.config.max_tokens,
        )
        return self._make_result(job.job_id, content)

    def handle_revision(self, job: Job, original: str, feedback: str) -> WorkResult:
        """Handle a revision request from the requester."""
        messages = [
            {"role": "user", "content": self._job_prompt(job)},
            {"role": "assistant", "content": _truncate_at_line(original, 4000)},
            {"role": "user", "content": f"The requester has requested changes:\n\n{feedback}\n\nPlease revise the deliverable accordingly."},
        ]
        content = self.claude.create_conversation(
            system=WORK_SYSTEM,
            messages=messages,
            max_tokens=self.config.max_tokens,
        )
        return self._make_result(job.job_id, content)

    async def complete_job_async(self, job: Job) -> WorkResult:
        return await asyncio.to_thread(self.complete_job, job)

    async def handle_revision_async(self, job: Job, original: str, feedback: str) -> WorkResult:
        return await asyncio.to_thread(self.handle_revision, job, original, feedback)
