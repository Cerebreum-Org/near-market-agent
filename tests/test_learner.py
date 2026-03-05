"""Tests for the self-improvement learner module."""

import json
import tempfile
from pathlib import Path

import pytest

from near_market_agent.learner import Learner, JobOutcome, AgentStats, LearningInsight


def _make_outcome(
    job_id: str = "test-job",
    status: str = "bid_pending",
    tier: str = "package",
    bid_amount: float = 4.0,
    budget_near: float = 5.0,
    **kwargs,
) -> JobOutcome:
    return JobOutcome(
        job_id=job_id,
        title=f"Test Job {job_id}",
        budget_near=budget_near,
        tier=tier,
        bid_amount=bid_amount,
        status=status,
        bid_at="2026-03-04T00:00:00Z",
        **kwargs,
    )


class TestJobOutcome:
    def test_create(self):
        o = _make_outcome()
        assert o.job_id == "test-job"
        assert o.status == "bid_pending"
        assert o.earned_near == 0.0

    def test_defaults(self):
        o = _make_outcome()
        assert o.revision_count == 0
        assert o.review_scores == []
        assert o.tags == []


class TestLearner:
    def test_init_creates_dir(self):
        with tempfile.TemporaryDirectory() as d:
            log_dir = Path(d) / "subdir"
            learner = Learner(log_dir=str(log_dir))
            assert log_dir.exists()

    def test_record_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            o = _make_outcome(job_id="abc123")
            learner.record_outcome(o)

            # Verify JSONL written
            outcomes_file = Path(d) / "outcomes.jsonl"
            assert outcomes_file.exists()
            lines = outcomes_file.read_text().strip().split("\n")
            assert len(lines) == 1

            # Reload and verify
            learner2 = Learner(log_dir=d)
            assert len(learner2._outcomes) == 1
            assert learner2._outcomes[0].job_id == "abc123"

    def test_update_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            learner.record_outcome(_make_outcome(job_id="j1", status="bid_pending"))
            learner.update_outcome("j1", status="accepted", earned_near=5.0)

            assert learner._outcomes[0].status == "accepted"
            assert learner._outcomes[0].earned_near == 5.0

            # Reload and verify persistence
            learner2 = Learner(log_dir=d)
            assert learner2._outcomes[0].status == "accepted"

    def test_update_nonexistent_job(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            learner.update_outcome("nonexistent", status="accepted")  # no crash


class TestComputeStats:
    def test_empty(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            stats = learner.compute_stats()
            assert stats.total_bids == 0
            assert stats.win_rate == 0

    def test_basic_stats(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            learner.record_outcome(_make_outcome("j1", status="accepted", earned_near=5.0))
            learner.record_outcome(_make_outcome("j2", status="bid_rejected"))
            learner.record_outcome(_make_outcome("j3", status="bid_pending"))

            stats = learner.compute_stats()
            assert stats.total_bids == 3
            assert stats.bids_accepted == 1
            assert stats.bids_rejected == 1
            assert stats.bids_pending == 1
            assert stats.total_earned_near == 5.0
            assert stats.win_rate == pytest.approx(1 / 3)

    def test_streak(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            learner.record_outcome(_make_outcome("j1", status="bid_rejected"))
            learner.record_outcome(_make_outcome("j2", status="accepted"))
            learner.record_outcome(_make_outcome("j3", status="accepted"))
            learner.record_outcome(_make_outcome("j4", status="accepted"))

            stats = learner.compute_stats()
            assert stats.streak == 3

    def test_avg_bid(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            learner.record_outcome(_make_outcome("j1", bid_amount=3.0))
            learner.record_outcome(_make_outcome("j2", bid_amount=5.0))

            stats = learner.compute_stats()
            assert stats.avg_bid_amount == pytest.approx(4.0)


class TestStatsMarkdown:
    def test_renders(self):
        stats = AgentStats(
            total_bids=10, bids_accepted=7, total_earned_near=35.0,
            win_rate=0.7, acceptance_rate=0.85,
        )
        md = stats.to_markdown()
        assert "35.0 NEAR" in md
        assert "70%" in md


class TestAnalyzePatterns:
    def test_insufficient_data(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            for i in range(3):
                learner.record_outcome(_make_outcome(f"j{i}"))
            insights = learner.analyze_patterns()
            assert insights == []

    def test_pricing_insight(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            # Accepted bids at low prices
            for i in range(3):
                learner.record_outcome(_make_outcome(f"a{i}", status="accepted", bid_amount=3.0))
            # Rejected bids at high prices
            for i in range(3):
                learner.record_outcome(_make_outcome(f"r{i}", status="bid_rejected", bid_amount=8.0))

            insights = learner.analyze_patterns()
            pricing = [i for i in insights if i.category == "pricing"]
            assert len(pricing) >= 1
            assert "lower" in pricing[0].action.lower() or "pricing" in pricing[0].action.lower()


class TestPricingSuggestion:
    def test_insufficient_data(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            assert learner.get_pricing_suggestion(5.0, "package") is None

    def test_suggests_based_on_history(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(log_dir=d)
            # Historically accepted at ~80% of budget
            for i in range(5):
                learner.record_outcome(_make_outcome(
                    f"j{i}", status="accepted", bid_amount=4.0, budget_near=5.0, tier="package",
                ))

            suggestion = learner.get_pricing_suggestion(10.0, "package")
            assert suggestion is not None
            assert 7.0 <= suggestion <= 9.0  # ~80% of 10
