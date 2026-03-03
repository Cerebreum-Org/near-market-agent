# 🤖 NEAR Market Agent

> Autonomous agent that finds jobs, bids, completes work, and submits deliverables on [market.near.ai](https://market.near.ai).

Built for the **[COMPETITION] Build the Most Useful Agent for market.near.ai** — 100 NEAR prize pool.

## What It Does

This agent handles the **full job lifecycle** autonomously:

```
Scan → Evaluate → Bid → Win → Complete Work → Submit → Get Paid
```

1. **Scans** open jobs on market.near.ai, sorted by budget
2. **Evaluates** each job using Claude — scores capability match, budget, competition, timeline
3. **Bids** on the best opportunities with tailored proposals
4. **Monitors** bid status, polling for awards
5. **Completes work** using Claude for research, writing, code, and analysis
6. **Submits deliverables** directly to the marketplace
7. **Handles revisions** if the requester requests changes
8. **Loops forever** — finding new jobs, managing active work, learning from outcomes

## Architecture

```
near_market_agent/
├── cli.py              # Click CLI — run, scan, status, bid, work
├── agent.py            # Core autonomous loop + state management
├── market_client.py    # Async httpx client for market.near.ai API
├── job_evaluator.py    # LLM-powered job scoring + proposal generation
├── work_engine.py      # LLM-powered work completion
├── models.py           # Pydantic models for all API objects
├── config.py           # Environment-based configuration
└── logger.py           # Structured logging (JSON files + rich console)
```

### Smart Job Selection

The agent uses a two-stage filter:

**Stage 1 — Rule-based preflight** (free, instant):
- Skips jobs under minimum budget threshold
- Skips multimedia creation (video, image, audio)
- Skips physical tasks (delivery, photography)
- Skips jobs requiring social media accounts
- Skips obvious trolls

**Stage 2 — LLM evaluation** (Claude, per-job):
- Scores 0-1 on capability match
- Generates tailored bid proposal
- Suggests bid amount and ETA
- Categorizes as research/writing/code/analysis/content

### Capability Profile

The agent knows it can handle:
- **Research & Analysis** — web research, competitive intel, market landscapes
- **Technical Writing** — blog posts, docs, tutorials, deep-dives, SEO
- **Code** — Python, JavaScript/TypeScript, Rust, Solidity (via Claude)
- **Content Creation** — marketing copy, social media, newsletters
- **Data Processing** — CSV, JSON, API pipelines, data analysis

## Quick Start

### Prerequisites
- Python 3.11+
- market.near.ai account with API key
- Anthropic API key

### Install

```bash
git clone https://github.com/Cerebreum-Org/near-market-agent.git
cd near-market-agent
pip install -e .
```

### Configure

```bash
export NEAR_MARKET_API_KEY=sk_live_...
export ANTHROPIC_API_KEY=sk-ant-...

# Optional
export MIN_BUDGET_NEAR=1.0          # Skip cheap jobs (default: 1.0)
export MAX_CONCURRENT_JOBS=3        # Parallel job limit (default: 3)
export POLL_INTERVAL=60             # Seconds between cycles (default: 60)
export BID_THRESHOLD=0.6            # Min eval score to bid (default: 0.6)
export CLAUDE_MODEL=claude-sonnet-4-20250514  # LLM model
```

### Run

```bash
# Full autonomous mode
near-agent run

# Dry run — evaluate jobs without bidding
near-agent --dry-run run

# One-shot scan — see what's available
near-agent scan

# Check agent status
near-agent status

# Bid on a specific job
near-agent bid JOB_ID --amount 4.0 --eta 24

# Complete work for an awarded job
near-agent work JOB_ID
```

### Demo

```bash
export NEAR_MARKET_API_KEY=sk_live_...
export ANTHROPIC_API_KEY=sk-ant-...
python demo/run_demo.py
```

## Design Decisions

### Why async?
The market API can be slow. Async lets us poll multiple jobs and manage concurrent work without blocking.

### Why Claude for evaluation AND work?
Same model that evaluates "can I do this?" also does the work. Consistent capability awareness — it doesn't bid on things it can't deliver.

### Why two-stage filtering?
LLM calls cost money. The preflight filter catches obvious skips (physical tasks, multimedia, trolls) before burning tokens on evaluation.

### Why structured logging?
The competition requires "demo logs showing agent in action." JSON logs are parseable, rich console output is readable. Both generated simultaneously.

### State persistence
The agent saves seen jobs, active bids, and completed work to disk. Survives restarts without re-bidding on old jobs.

## Example Output

```
🚀 Starting autonomous agent loop
⚡ Authenticated as cerebreum (balance: 0.0 NEAR)
── Cycle 1 ──
ℹ Found 48 open jobs
ℹ Evaluating 48 new jobs

┌──────────────────── Job Scan Results ────────────────────┐
│ Score │  Budget │ Bids │ Category │ Title           │ Bid │
│ 0.85  │ 5.0 NEAR│   88 │ writing  │ TEE + NEAR AI…  │ ✅  │
│ 0.80  │ 4.0 NEAR│   99 │ writing  │ How AI agents…  │ ✅  │
│ 0.75  │ 3.0 NEAR│   79 │ research │ Map AI Agent…   │ ✅  │
│ 0.00  │ 2.0 NEAR│    2 │ skip     │ Nuclear warhe…  │ ❌  │
└──────────────────────────────────────────────────────────┘

🤔 [DRY RUN] Would bid 4.5 NEAR on: Write blog post: TEE + NEAR AI…
📤 Bid placed: 4.5 NEAR on "Write blog post: TEE + NEAR AI…"
```

## Competition Criteria

| Criterion | Weight | How We Score |
|-----------|--------|-------------|
| **Usefulness** | 40% | Full lifecycle automation — scan, bid, work, submit. Actually usable. |
| **Code quality** | 25% | Type hints, Pydantic models, async/await, structured logging, clean architecture |
| **Autonomy** | 20% | Runs unattended, handles errors, persists state, manages multiple jobs |
| **Creativity** | 15% | Two-stage smart filtering, LLM-powered proposals, automatic revision handling |

## License

MIT

## Built By

[Cerebreum](https://github.com/Cerebreum-Org) — autonomous agent infrastructure
