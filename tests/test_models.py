"""Unit tests for pydantic models and computed properties."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from near_market_agent.models import Bid, Job, JobEvaluation, WalletBalance


class ModelsTests(unittest.TestCase):
    def test_job_budget_near_parses_or_falls_back(self) -> None:
        valid = Job(
            job_id="j1",
            creator_agent_id="a1",
            title="T",
            description="D",
            budget_amount="3.5",
        )
        invalid = Job(
            job_id="j2",
            creator_agent_id="a2",
            title="T",
            description="D",
            budget_amount="oops",
        )
        missing = Job(
            job_id="j3",
            creator_agent_id="a3",
            title="T",
            description="D",
            budget_amount=None,
        )

        self.assertEqual(valid.budget_near, 3.5)
        self.assertEqual(invalid.budget_near, 0.0)
        self.assertEqual(missing.budget_near, 0.0)

    def test_job_is_expired_handles_naive_and_aware_datetimes(self) -> None:
        past_aware = datetime.now(timezone.utc) - timedelta(minutes=1)
        # Code treats naive datetimes as UTC. Build a naive UTC timestamp.
        future_naive = (datetime.now(timezone.utc) + timedelta(minutes=1)).replace(tzinfo=None)

        expired = Job(
            job_id="ja",
            creator_agent_id="a1",
            title="T",
            description="D",
            expires_at=past_aware,
        )
        active = Job(
            job_id="jb",
            creator_agent_id="a1",
            title="T",
            description="D",
            expires_at=future_naive,
        )

        self.assertTrue(expired.is_expired)
        self.assertFalse(active.is_expired)

    def test_bid_and_wallet_amounts(self) -> None:
        bid_ok = Bid(bid_id="b1", job_id="j1", bidder_agent_id="a1", amount="1.75")
        bid_bad = Bid(bid_id="b2", job_id="j1", bidder_agent_id="a1", amount="x")
        wallet_ok = WalletBalance(balance="99.9")
        wallet_bad = WalletBalance(balance="nanx")

        self.assertEqual(bid_ok.amount_near, 1.75)
        self.assertEqual(bid_bad.amount_near, 0.0)
        self.assertEqual(wallet_ok.amount, 99.9)
        self.assertEqual(wallet_bad.amount, 0.0)

    def test_job_evaluation_enforces_score_range(self) -> None:
        with self.assertRaises(ValidationError):
            JobEvaluation(job_id="j1", score=1.2)
