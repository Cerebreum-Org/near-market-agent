"""Self-improvement engine — learns from job outcomes to get better over time.

Tracks every bid, completion, and outcome. Analyzes patterns in accepted vs
rejected work to improve future proposals, bid pricing, and build quality.

This is what makes the agent smarter the longer it runs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)


@dataclass
class JobOutcome:
    """Record of a single job attempt."""
    job_id: str
    title: str
    budget_near: float
    tier: str
    bid_amount: float
    status: Literal["bid_pending", "bid_rejected", "awarded", "submitted",
                     "accepted", "revision_requested", "disputed", "expired"]
    bid_at: str  # ISO timestamp
    completed_at: str | None = None
    earned_near: float = 0.0
    revision_count: int = 0
    build_time_seconds: float = 0.0
    test_passed: bool = False
    test_count: int = 0
    review_scores: list[float] = field(default_factory=list)
    rejection_reason: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class AgentStats:
    """Aggregated performance statistics."""
    total_bids: int = 0
    bids_accepted: int = 0
    bids_rejected: int = 0
    bids_pending: int = 0
    jobs_completed: int = 0
    jobs_accepted: int = 0
    jobs_disputed: int = 0
    total_earned_near: float = 0.0
    total_revisions: int = 0
    avg_review_score: float = 0.0
    avg_bid_amount: float = 0.0
    avg_build_time_seconds: float = 0.0
    win_rate: float = 0.0  # accepted / total bids
    acceptance_rate: float = 0.0  # jobs accepted / jobs submitted
    best_tier: str = ""  # tier with highest acceptance rate
    worst_tier: str = ""  # tier with lowest acceptance rate
    streak: int = 0  # consecutive accepted jobs

    def to_markdown(self) -> str:
        """Render stats as a markdown summary."""
        lines = [
            "# Agent Performance Dashboard",
            "",
            f"**Total Earned:** {self.total_earned_near:.1f} NEAR",
            f"**Win Rate:** {self.win_rate:.0%} ({self.bids_accepted}/{self.total_bids} bids)",
            f"**Acceptance Rate:** {self.acceptance_rate:.0%} ({self.jobs_accepted}/{self.jobs_completed} submitted)",
            "",
            "## Bids",
            f"- Pending: {self.bids_pending}",
            f"- Accepted: {self.bids_accepted}",
            f"- Rejected: {self.bids_rejected}",
            "",
            "## Work Quality",
            f"- Avg Review Score: {self.avg_review_score:.2f}/1.0",
            f"- Avg Build Time: {self.avg_build_time_seconds:.0f}s",
            f"- Total Revisions: {self.total_revisions}",
            f"- Current Streak: {self.streak} accepted",
            "",
            "## Tier Performance",
            f"- Best Tier: {self.best_tier or 'N/A'}",
            f"- Worst Tier: {self.worst_tier or 'N/A'}",
        ]
        return "\n".join(lines)


@dataclass
class LearningInsight:
    """An insight extracted from outcome analysis."""
    category: str  # "pricing", "proposal", "quality", "tier", "timing"
    insight: str
    confidence: float  # 0-1
    action: str  # what to do differently


class Learner:
    """Tracks outcomes and extracts learning insights.

    Persists a JSONL log of all job outcomes and computes aggregate
    stats. Periodically analyzes patterns to produce actionable insights.
    """

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._outcomes_file = self.log_dir / "outcomes.jsonl"
        self._insights_file = self.log_dir / "insights.json"
        self._stats_file = self.log_dir / "stats.json"
        self._outcomes: list[JobOutcome] = []
        self._load()

    def _load(self):
        """Load outcomes from JSONL file."""
        if not self._outcomes_file.exists():
            return
        try:
            for line in self._outcomes_file.read_text().splitlines():
                if line.strip():
                    data = json.loads(line)
                    self._outcomes.append(JobOutcome(**data))
            log.info(f"Loaded {len(self._outcomes)} historical outcomes")
        except (json.JSONDecodeError, TypeError) as e:
            log.warning(f"Failed to load outcomes: {e}")

    def record_outcome(self, outcome: JobOutcome):
        """Record a new job outcome."""
        self._outcomes.append(outcome)
        # Append to JSONL (crash-safe)
        with open(self._outcomes_file, "a") as f:
            f.write(json.dumps(asdict(outcome)) + "\n")
        log.info(f"Recorded outcome: {outcome.title[:40]} → {outcome.status}")
        # Recompute stats
        self._save_stats()

    def update_outcome(self, job_id: str, **updates):
        """Update an existing outcome by job_id."""
        for o in reversed(self._outcomes):
            if o.job_id == job_id:
                for k, v in updates.items():
                    if hasattr(o, k):
                        setattr(o, k, v)
                # Rewrite full file (safe — outcomes are small)
                self._rewrite_outcomes()
                self._save_stats()
                log.info(f"Updated outcome {job_id[:8]}: {updates}")
                return
        log.warning(f"Outcome not found for job {job_id[:8]}")

    def _rewrite_outcomes(self):
        """Rewrite the full outcomes file."""
        with open(self._outcomes_file, "w") as f:
            for o in self._outcomes:
                f.write(json.dumps(asdict(o)) + "\n")

    def compute_stats(self) -> AgentStats:
        """Compute aggregate performance statistics."""
        stats = AgentStats()
        if not self._outcomes:
            return stats

        stats.total_bids = len(self._outcomes)
        stats.bids_accepted = sum(1 for o in self._outcomes if o.status in ("awarded", "submitted", "accepted", "revision_requested"))
        stats.bids_rejected = sum(1 for o in self._outcomes if o.status == "bid_rejected")
        stats.bids_pending = sum(1 for o in self._outcomes if o.status == "bid_pending")
        stats.jobs_completed = sum(1 for o in self._outcomes if o.status in ("submitted", "accepted", "revision_requested", "disputed"))
        stats.jobs_accepted = sum(1 for o in self._outcomes if o.status == "accepted")
        stats.jobs_disputed = sum(1 for o in self._outcomes if o.status == "disputed")
        stats.total_earned_near = sum(o.earned_near for o in self._outcomes)
        stats.total_revisions = sum(o.revision_count for o in self._outcomes)

        # Averages
        bid_amounts = [o.bid_amount for o in self._outcomes if o.bid_amount > 0]
        stats.avg_bid_amount = sum(bid_amounts) / len(bid_amounts) if bid_amounts else 0

        review_scores = [s for o in self._outcomes for s in o.review_scores]
        stats.avg_review_score = sum(review_scores) / len(review_scores) if review_scores else 0

        build_times = [o.build_time_seconds for o in self._outcomes if o.build_time_seconds > 0]
        stats.avg_build_time_seconds = sum(build_times) / len(build_times) if build_times else 0

        # Rates
        stats.win_rate = stats.bids_accepted / stats.total_bids if stats.total_bids else 0
        stats.acceptance_rate = stats.jobs_accepted / stats.jobs_completed if stats.jobs_completed else 0

        # Tier analysis
        tier_stats: dict[str, dict] = {}
        for o in self._outcomes:
            if o.tier not in tier_stats:
                tier_stats[o.tier] = {"total": 0, "accepted": 0}
            tier_stats[o.tier]["total"] += 1
            if o.status == "accepted":
                tier_stats[o.tier]["accepted"] += 1

        if tier_stats:
            rates = {t: s["accepted"] / s["total"] for t, s in tier_stats.items() if s["total"] >= 2}
            if rates:
                stats.best_tier = max(rates, key=rates.get)
                stats.worst_tier = min(rates, key=rates.get)

        # Streak
        streak = 0
        for o in reversed(self._outcomes):
            if o.status == "accepted":
                streak += 1
            elif o.status in ("disputed", "bid_rejected"):
                break
        stats.streak = streak

        return stats

    def _save_stats(self):
        """Save computed stats to disk."""
        stats = self.compute_stats()
        self._stats_file.write_text(json.dumps(asdict(stats), indent=2))

    def analyze_patterns(self) -> list[LearningInsight]:
        """Analyze outcome patterns and produce actionable insights.

        Called periodically to extract learning from accumulated data.
        """
        insights: list[LearningInsight] = []
        if len(self._outcomes) < 5:
            return insights

        # Pricing insights
        accepted = [o for o in self._outcomes if o.status in ("awarded", "accepted")]
        rejected = [o for o in self._outcomes if o.status == "bid_rejected"]
        if accepted and rejected:
            avg_accepted_bid = sum(o.bid_amount for o in accepted) / len(accepted)
            avg_rejected_bid = sum(o.bid_amount for o in rejected) / len(rejected)
            if avg_rejected_bid > avg_accepted_bid * 1.3:
                insights.append(LearningInsight(
                    category="pricing",
                    insight=f"Rejected bids average {avg_rejected_bid:.1f} NEAR vs accepted {avg_accepted_bid:.1f} NEAR",
                    confidence=0.7,
                    action="Lower bid amounts — we may be pricing ourselves out",
                ))
            elif avg_rejected_bid < avg_accepted_bid * 0.7:
                insights.append(LearningInsight(
                    category="pricing",
                    insight=f"Rejected bids average {avg_rejected_bid:.1f} NEAR vs accepted {avg_accepted_bid:.1f} NEAR",
                    confidence=0.6,
                    action="Higher bids might signal quality — don't undercut too much",
                ))

        # Quality insights
        revised = [o for o in self._outcomes if o.revision_count > 0]
        if revised and len(revised) > len(self._outcomes) * 0.3:
            common_tiers = {}
            for o in revised:
                common_tiers[o.tier] = common_tiers.get(o.tier, 0) + 1
            worst = max(common_tiers, key=common_tiers.get) if common_tiers else None
            if worst:
                insights.append(LearningInsight(
                    category="quality",
                    insight=f"{len(revised)}/{len(self._outcomes)} jobs needed revision, especially {worst} tier",
                    confidence=0.8,
                    action=f"Improve {worst} tier builder agent — check common failure modes",
                ))

        # Tier insights
        tier_wins: dict[str, list] = {}
        for o in self._outcomes:
            if o.tier not in tier_wins:
                tier_wins[o.tier] = []
            tier_wins[o.tier].append(o.status == "accepted")
        for tier, results in tier_wins.items():
            if len(results) >= 3:
                rate = sum(results) / len(results)
                if rate < 0.2:
                    insights.append(LearningInsight(
                        category="tier",
                        insight=f"{tier} tier has {rate:.0%} acceptance rate ({sum(results)}/{len(results)})",
                        confidence=0.7,
                        action=f"Consider disabling {tier} tier or improving its builder agent",
                    ))

        # Save insights
        self._insights_file.write_text(
            json.dumps([asdict(i) for i in insights], indent=2)
        )
        log.info(f"Generated {len(insights)} learning insights")
        return insights

    def get_pricing_suggestion(self, job_budget: float, tier: str) -> float | None:
        """Suggest a bid amount based on historical data for this tier.

        Returns None if insufficient data.
        """
        tier_accepted = [
            o for o in self._outcomes
            if o.tier == tier and o.status in ("awarded", "accepted")
        ]
        if len(tier_accepted) < 3:
            return None

        # Bid at the average ratio of (bid / budget) that got accepted
        ratios = [o.bid_amount / o.budget_near for o in tier_accepted if o.budget_near > 0]
        if not ratios:
            return None

        avg_ratio = sum(ratios) / len(ratios)
        suggested = job_budget * avg_ratio
        log.info(f"Pricing suggestion for {tier} ({job_budget} NEAR): {suggested:.1f} NEAR (avg ratio {avg_ratio:.2f})")
        return round(suggested, 1)
