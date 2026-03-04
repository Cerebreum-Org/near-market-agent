# NEAR Market Agent

An autonomous agent that earns NEAR by completing jobs on [market.near.ai](https://market.near.ai).

## How it works

```
Scan → Evaluate → Bid → Work → Submit → Get Paid
```

The agent runs in a loop. Each cycle it scans for open jobs, scores them, bids on the best ones, completes awarded work, and submits deliverables. If a requester asks for revisions, it reads the feedback and resubmits. That's it.

### The core loop

```python
while running:
    await check_active_bids()      # did we win anything?
    await check_active_jobs()       # any work to do or revisions requested?

    if len(active_jobs) < max_concurrent:
        await scan_and_bid()        # find new opportunities

    sleep(poll_interval)
```

### Smart filtering

Not every job is worth bidding on. A fast rule-based preflight filter catches obvious skips (physical tasks, multimedia creation, trolls) before spending tokens on LLM evaluation. Jobs that pass preflight get scored 0–1 by Claude on capability match, budget, and competition.

### Work completion

When a bid is accepted, the agent reads the job description, thinks about it with Claude, and produces a deliverable. The output is saved locally before submission — if the API call fails, the work isn't lost.

## Setup

```bash
git clone https://github.com/Cerebreum-Org/near-market-agent.git
cd near-market-agent
pip install -e .
```

```bash
export NEAR_MARKET_API_KEY=sk_live_...
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
near-agent run                              # autonomous mode (persistent loop)
near-agent run -i 120                       # custom poll interval (seconds)
near-agent run --dry-run                    # evaluate without bidding
near-agent scan                             # one-shot job scan
near-agent status                           # check profile + balance
near-agent bid JOB_ID --amount 4 --eta 24   # manual bid
near-agent work JOB_ID                      # complete a specific job
```

## Deployment — Always-On with tmux

The agent is designed to run persistently. It handles `SIGINT`/`SIGTERM` gracefully, persists state to disk between cycles, and resumes cleanly after restart.

```bash
# Start a persistent session
tmux new-session -d -s near-agent
tmux send-keys -t near-agent 'cd ~/near-market-agent && NEAR_MARKET_API_KEY=$(security find-generic-password -s "market.near.ai" -a "cerebreum" -w) uv run near-agent run -i 120' Enter

# Attach to watch it work
tmux attach -t near-agent

# Detach without stopping: Ctrl+B, then D
```

**What happens each cycle:**
1. Check active bids → did any get accepted?
2. Check active jobs → work submitted? revision requested?
3. Scan for new jobs → preflight filter → LLM evaluate → bid
4. Save state → sleep → repeat

**State persists** in `logs/agent_state.json` — tracks seen jobs, active bids, completed work. Survives restarts cleanly.

**Graceful shutdown:** Send `SIGTERM` or `Ctrl+C`. Agent finishes its current cycle, saves state, then exits. No orphaned work.

## Configuration

All optional. Sane defaults included.

| Variable | Default | What it does |
|----------|---------|-------------|
| `MIN_BUDGET_NEAR` | `1.0` | Skip jobs below this budget |
| `MAX_CONCURRENT_JOBS` | `3` | Parallel job limit |
| `POLL_INTERVAL` | `60` | Seconds between scan cycles |
| `BID_THRESHOLD` | `0.6` | Minimum score to bid |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | LLM model |

## Project structure

```
near_market_agent/
├── agent.py           # Core loop + state management
├── market_client.py   # Async API client with retry
├── job_evaluator.py   # Two-stage job scoring
├── work_engine.py     # LLM-powered work completion
├── models.py          # Data models
├── config.py          # Environment config
├── cli.py             # CLI interface
└── logger.py          # Structured logging
```

~1,500 lines total. 23 tests.

## What it can do

- Research, analysis, competitive intel
- Technical writing, blog posts, documentation
- Code in Python, TypeScript, Rust, Solidity
- Marketing copy, content, newsletters
- Data processing and API integration

## What it won't bid on

- Jobs requiring physical presence
- Video, image, or audio production
- Social media account access
- Anything below budget threshold
- Obviously fake or malicious postings

## License

MIT

## Built by

[Cerebreum](https://github.com/Cerebreum-Org)
