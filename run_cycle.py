#!/usr/bin/env python3
"""Run a single agent cycle — scan, evaluate, bid, check active work."""

import asyncio
import sys
from near_market_agent.config import Config
from near_market_agent.agent import MarketAgent


async def main():
    cfg = Config.from_env()
    cfg.verbose = True
    cfg.log_dir = "logs"

    errors = cfg.validate()
    if errors:
        print(f"Config errors: {errors}", file=sys.stderr)
        sys.exit(1)

    agent = MarketAgent(cfg)
    async with agent.client:
        await agent._check_identity()

        # Check existing bids/jobs first
        await agent._check_active_bids()
        await agent._check_active_jobs()

        # Scan for new opportunities
        if len(agent._active_jobs) < cfg.max_concurrent_jobs:
            await agent._scan_and_bid()

        agent._save_state()

asyncio.run(main())
