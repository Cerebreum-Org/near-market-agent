"""Pydantic models for market.near.ai API objects."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _safe_float(value: str | None) -> float:
    """Parse a string to float, returning 0.0 on failure."""
    try:
        return float(value) if value else 0.0
    except (ValueError, TypeError):
        return 0.0


class JobStatus(StrEnum):
    OPEN = "open"
    FILLING = "filling"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CLOSED = "closed"
    EXPIRED = "expired"
    JUDGING = "judging"


class JobType(StrEnum):
    STANDARD = "standard"
    COMPETITION = "competition"


class BidStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class AssignmentStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    CANCELLED = "cancelled"


class AgentProfile(BaseModel):
    agent_id: str
    near_account_id: str | None = None
    handle: str | None = None
    capabilities: dict[str, Any] | None = None
    status: str | None = None
    created_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    reputation: int | None = None
    earned: float | None = None
    completed_jobs: int | None = None
    total_bids: int | None = None


class Job(BaseModel):
    job_id: str
    creator_agent_id: str
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    budget_amount: str | None = None
    budget_token: str | None = "NEAR"
    requires_verifiable: bool = False
    job_type: JobType = JobType.STANDARD
    status: JobStatus = JobStatus.OPEN
    awarded_bid_id: str | None = None
    worker_agent_id: str | None = None
    deliverable: str | None = None
    deliverable_hash: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    dispute_agent_id: str | None = None
    max_slots: int = 1
    current_max_slots: int | None = None
    filled_slots: int | None = None
    bid_count: int | None = None
    creator_reputation: int | None = None
    my_assignments: list[dict[str, Any]] | None = None

    @property
    def budget_near(self) -> float:
        return _safe_float(self.budget_amount)

    @property
    def is_expired(self) -> bool:
        if self.expires_at:
            expires = (
                self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=UTC)
            )
            return datetime.now(UTC) > expires
        return False


class Bid(BaseModel):
    bid_id: str
    job_id: str
    bidder_agent_id: str
    amount: str
    eta_seconds: int | None = None
    proposal: str | None = None
    status: BidStatus = BidStatus.PENDING
    created_at: datetime | None = None

    @property
    def amount_near(self) -> float:
        return _safe_float(self.amount)


class Assignment(BaseModel):
    assignment_id: str
    status: AssignmentStatus
    deliverable: str | None = None
    deliverable_hash: str | None = None
    submitted_at: datetime | None = None
    escrow_amount: str | None = None


class Message(BaseModel):
    message_id: str
    sender_agent_id: str
    content: str
    created_at: datetime | None = None


class WalletBalance(BaseModel):
    balance: str
    currency: str = "NEAR"

    @property
    def amount(self) -> float:
        return _safe_float(self.balance)


class JobEvaluation(BaseModel):
    """LLM evaluation of a job opportunity."""

    job_id: str
    score: float = Field(
        ge=0, le=1, description="0-1 score of how well this matches our capabilities"
    )
    should_bid: bool = False
    reasoning: str = ""
    suggested_bid_amount: float | None = None
    suggested_eta_hours: int | None = None
    proposal_draft: str = ""
    category: str = ""  # research, writing, code, analysis, content, skip
