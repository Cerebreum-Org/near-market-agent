#!/usr/bin/env python3
"""Demo script — runs agent in dry-run mode against the live API.

Shows the agent scanning jobs, evaluating them, and deciding which to bid on
without actually placing any bids.

Usage:
    export NEAR_MARKET_API_KEY=sk_live_...
    python demo/run_demo.py

Note: Uses Claude CLI (`claude -p`) for LLM calls — no Anthropic API key needed.
"""

import asyncio
import sys
import os

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from near_market_agent.config import Config
from near_market_agent.agent import MarketAgent


async def main():
    config = Config.from_env()
    config.dry_run = True
    config.verbose = True
    config.log_dir = "demo/logs"

    errors = config.validate()
    if errors:
        print(f"Config errors: {errors}")
        print("Set NEAR_MARKET_API_KEY environment variable")
        sys.exit(1)

    agent = MarketAgent(config)

    print("=" * 60)
    print("🤖 NEAR Market Agent — Demo Run (DRY RUN)")
    print("=" * 60)

    # Show status
    print("\n📊 Agent Status:")
    await agent.status()

    # Scan and evaluate
    print("\n🔍 Scanning open jobs...")
    jobs, evals = await agent.scan()

    # Show top opportunities
    bidworthy = sorted(
        [e for e in evals if e.should_bid],
        key=lambda e: e.score,
        reverse=True,
    )

    if bidworthy:
        print(f"\n🎯 Top {min(5, len(bidworthy))} opportunities:")
        for i, ev in enumerate(bidworthy[:5], 1):
            job = next((j for j in jobs if j.job_id == ev.job_id), None)
            if job:
                print(f"\n  {i}. [{ev.score:.2f}] {job.title[:60]}")
                print(f"     Budget: {job.budget_near} NEAR | Bids: {job.bid_count}")
                print(f"     Category: {ev.category}")
                print(f"     Proposal preview: {ev.proposal_draft[:100]}...")
    else:
        print("\n❌ No jobs worth bidding on found")

    print(f"\n📁 Logs saved to: {config.log_dir}/")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
