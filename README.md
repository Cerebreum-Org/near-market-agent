# NEAR Market Agent

An autonomous agent that earns NEAR by completing jobs on [market.near.ai](https://market.near.ai).

## How it works

```
Scan → Evaluate → Bid → Work → Submit → Get Paid
```

The agent runs in a loop. Each cycle it scans for open jobs, scores them, bids on the best ones, completes awarded work using specialized AI builders, and submits deliverables. If a requester asks for revisions, it reads the feedback and resubmits.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Main Loop (agent.py)                │
│                                                         │
│  scan_and_bid() → check_active_bids() → check_jobs()   │
│       │                    │                  │         │
│       ▼                    ▼                  ▼         │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │Evaluator │    │Market Client │    │ Work Engine  │   │
│  │(scoring) │    │  (async API) │    │  (building)  │   │
│  └────┬─────┘    └──────────────┘    └──────┬───────┘   │
│       │                                     │           │
│       ▼                                     ▼           │
│  ┌──────────┐                    ┌────────────────────┐ │
│  │Job Router│                    │  Claude Code CLI   │ │
│  │(classify)│                    │  (agentic builds)  │ │
│  └──────────┘                    └────────┬───────────┘ │
│                                           │             │
│                              ┌────────────┼──────────┐  │
│                              ▼            ▼          ▼  │
│                         text-writer  pkg-builder  svc-  │
│                                                  builder│
└─────────────────────────────────────────────────────────┘
```

### Job tiers

The router classifies each job by keywords and tags — no LLM call:

| Tier | Share | Agent | What it builds |
|------|-------|-------|---------------|
| Text | 18% | `text-writer` | Guides, docs, technical writing |
| Package | 47% | `package-builder` | npm, pypi, MCP packages |
| Service | 34% | `service-builder` | Bots, extensions, APIs |
| System | 1% | `system-builder` | Multi-agent orchestration |

### Review pipeline

Every deliverable goes through 3 automated review stages before submission:
1. **Requirements check** — does it address the job description?
2. **Quality check** — code quality, completeness, best practices
3. **Final gate** — overall readiness, edge cases, polish

Failed reviews trigger automatic revision. Max 2 revision rounds per stage.

## Prerequisites

- **Python 3.11+**
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — the work engine uses Claude Code CLI for agentic building

Install Claude Code:
```bash
npm install -g @anthropic-ai/claude-code
```

Verify it works:
```bash
claude --version    # should print version
claude --help       # should show CLI options
```

Claude Code needs an Anthropic API key. Set it up:
```bash
# Option 1: Environment variable (Claude Code reads this automatically)
export ANTHROPIC_API_KEY=sk-ant-...

# Option 2: Run `claude` once interactively — it will prompt for auth
claude
```

## Setup

```bash
git clone https://github.com/Cerebreum-Org/near-market-agent.git
cd near-market-agent
pip install -e .
```

Copy the example env file and fill in your keys:
```bash
cp .env.example .env
# Edit .env with your values
```

Required environment variables:
```bash
# Your market.near.ai API key (get one at https://market.near.ai)
NEAR_MARKET_API_KEY=sk_live_...

# Anthropic API key (also used by Claude Code for agentic builds)
ANTHROPIC_API_KEY=sk-ant-...
```

Run the tests to make sure everything works:
```bash
pip install pytest
pytest -v
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

## Deployment — Always-On

The agent handles `SIGINT`/`SIGTERM` gracefully, persists state to disk between cycles, and resumes cleanly after restart.

### With tmux (any OS)

```bash
tmux new-session -d -s near-agent
tmux send-keys -t near-agent 'cd ~/near-market-agent && near-agent run -i 120' Enter

# Attach to watch it work
tmux attach -t near-agent

# Detach without stopping: Ctrl+B, then D
```

### With Docker

```bash
docker build -t near-market-agent .
docker run -d \
  --name near-agent \
  -e NEAR_MARKET_API_KEY=sk_live_... \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v near-agent-state:/app/logs \
  near-market-agent
```

### What happens each cycle

1. Check active bids → did any get accepted?
2. Check active jobs → work submitted? revision requested?
3. Scan for new jobs → preflight filter → LLM evaluate → bid
4. Save state → sleep → repeat

**State persists** in `logs/agent_state.json` — tracks seen jobs, active bids, completed work. Survives restarts.

**Graceful shutdown:** `Ctrl+C` or `SIGTERM`. Finishes current cycle, saves state, exits.

## Configuration

All optional. Sane defaults included.

| Variable | Default | What it does |
|----------|---------|-------------|
| `MIN_BUDGET_NEAR` | `1.0` | Skip jobs below this budget |
| `MAX_CONCURRENT_JOBS` | `3` | Parallel job limit |
| `POLL_INTERVAL` | `60` | Seconds between scan cycles |
| `BID_THRESHOLD` | `0.6` | Minimum score to bid (0-1) |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | LLM model for evaluation |
| `DRY_RUN` | `false` | Evaluate without placing bids |
| `LOG_DIR` | `logs` | Where state + logs are stored |

### Per-tier settings

| Variable | Default | What it does |
|----------|---------|-------------|
| `TIER_TEXT_TIMEOUT` | `300` | Text job build timeout (seconds) |
| `TIER_PACKAGE_TIMEOUT` | `600` | Package job build timeout |
| `TIER_SERVICE_TIMEOUT` | `900` | Service job build timeout |
| `TIER_SYSTEM_TIMEOUT` | `1200` | System job build timeout |
| `TIER_TEXT_MODEL` | *(none)* | Override model for text tier |
| `TIER_PACKAGE_MODEL` | *(none)* | Override model for package tier |
| `DISABLED_TIERS` | *(none)* | Comma-separated tiers to skip |

## Project structure

```
near_market_agent/
├── agent.py           # Core loop, state management, bid/job lifecycle
├── work_engine.py     # Agentic build pipeline, workspace, reviews
├── market_client.py   # Async API client with retry + rate limiting
├── job_evaluator.py   # Two-stage scoring (preflight + LLM)
├── job_router.py      # Keyword-based tier classification
├── claude_cli.py      # Claude Code CLI wrapper (prompt + agentic modes)
├── config.py          # Environment config + tier settings
├── models.py          # Pydantic data models
├── sanitize.py        # Prompt injection defense
├── json_utils.py      # Robust JSON extraction from LLM output
├── cli.py             # Click CLI interface
└── logger.py          # Structured logging

.claude/agents/        # Specialized Claude Code agent prompts
├── text-writer.md
├── package-builder.md
├── service-builder.md
└── system-builder.md

templates/             # Starter scaffolds for package jobs
├── mcp-server/
├── npm-package/
└── pypi-package/

knowledge/
└── near-reference.md  # NEAR protocol reference (RPC, SDKs, standards)
```

~2,800 lines of source. 120 tests.

## Security

- All job descriptions are sanitized before reaching LLM prompts (prompt injection defense)
- API keys are never logged or written to state files
- State files contain only job IDs and metadata, not secrets
- Workspace temp directories are cleaned up after each job (+ stale cleanup on startup)

## License

MIT

## Built by

[Cerebreum](https://github.com/Cerebreum-Org)
