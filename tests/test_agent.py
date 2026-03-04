"""Unit tests for the MarketAgent state management."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path

from near_market_agent.agent import MarketAgent
from near_market_agent.config import Config
from near_market_agent.models import Bid


class AgentStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.config = Config(
            market_api_key="mk",
            anthropic_api_key="ak",
            log_dir=self.tmpdir,
        )
        self.agent = MarketAgent(self.config)

    def test_save_and_load_state_roundtrips(self) -> None:
        self.agent._seen_jobs = OrderedDict([("j1", True), ("j2", True)])
        self.agent._bid_jobs = {"j1"}
        self.agent._completed = {"j0"}
        self.agent._revised_assignments = {"a1"}
        self.agent._active_bids = {
            "b1": Bid(
                bid_id="b1",
                job_id="j1",
                bidder_agent_id="agent-1",
                amount="5.0",
            )
        }

        self.agent._save_state()

        agent2 = MarketAgent(self.config)
        agent2._load_state()

        self.assertEqual(set(agent2._seen_jobs.keys()), {"j1", "j2"})
        self.assertEqual(agent2._bid_jobs, {"j1"})
        self.assertEqual(agent2._completed, {"j0"})
        self.assertEqual(agent2._revised_assignments, {"a1"})
        self.assertIn("b1", agent2._active_bids)
        self.assertEqual(agent2._active_bids["b1"].amount_near, 5.0)

    def test_load_state_handles_missing_file(self) -> None:
        self.agent._load_state()
        self.assertEqual(len(self.agent._seen_jobs), 0)

    def test_load_state_handles_corrupt_json(self) -> None:
        state_file = Path(self.tmpdir) / "agent_state.json"
        state_file.write_text("not json", encoding="utf-8")
        self.agent._load_state()
        self.assertEqual(len(self.agent._seen_jobs), 0)

    def test_save_state_uses_atomic_write(self) -> None:
        self.agent._save_state()
        state_file = Path(self.tmpdir) / "agent_state.json"
        tmp_file = Path(self.tmpdir) / "agent_state.tmp"

        self.assertTrue(state_file.exists())
        self.assertFalse(tmp_file.exists())

        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn("saved_at", data)

    def test_seen_jobs_eviction_preserves_order(self) -> None:
        """Oldest seen_jobs are evicted first (FIFO)."""
        self.agent.MAX_SEEN_JOBS = 5
        # Insert j1 through j7
        for i in range(1, 8):
            self.agent._seen_jobs[f"j{i}"] = True
        self.agent._evict_seen_jobs()

        # Should keep j3-j7 (last 5), evict j1-j2
        self.assertEqual(len(self.agent._seen_jobs), 5)
        self.assertNotIn("j1", self.agent._seen_jobs)
        self.assertNotIn("j2", self.agent._seen_jobs)
        self.assertIn("j3", self.agent._seen_jobs)
        self.assertIn("j7", self.agent._seen_jobs)

    def test_seen_jobs_state_roundtrip_preserves_order(self) -> None:
        """Seen jobs maintain insertion order through save/load."""
        self.agent._seen_jobs = OrderedDict([("a", True), ("b", True), ("c", True)])
        self.agent._save_state()

        agent2 = MarketAgent(self.config)
        agent2._load_state()

        keys = list(agent2._seen_jobs.keys())
        self.assertEqual(keys, ["a", "b", "c"])
