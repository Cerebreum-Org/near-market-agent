# NEAR Market Agent

An autonomous agent that earns NEAR by completing jobs on [market.near.ai](https://market.near.ai).

Clone it, configure your keys, and let it run. It scans for jobs, bids on ones it can handle, builds deliverables using AI, and submits them — all on autopilot.

## How It Works

```
Scan → Evaluate → Bid → Research → Build → Test → Review → Submit → Get Paid
```

Each cycle the agent:
1. **Scans** open jobs on market.near.ai
2. **Evaluates** each job with a two-stage scorer (keyword preflight + LLM assessment)
3. **Bids** on the best matches
4. **Researches** the job domain (web search, npm/pypi lookups, doc fetching)
5. **Builds** the deliverable using specialized Claude Code agents
6. **Tests** the output (runs `npm test`, `pytest`, or `cargo test`)
7. **Reviews** through a 3-stage pipeline (requirements → quality → final gate)
8. **Publishes** code to GitHub (if configured) and submits to the marketplace

### Architecture

```
┌───────────────────────────────────────────────────────────┐
│                     Main Loop (agent.py)                  │
│                                                           │
│  scan_and_bid() → check_active_bids() → check_jobs()     │
│       │                    │                  │           │
│       ▼                    ▼                  ▼           │
│  ┌──────────┐    ┌──────────────┐    ┌────────────────┐   │
│  │Evaluator │    │Market Client │    │  Work Engine   │   │
│  │(scoring) │    │  (async API) │    │   (pipeline)   │   │
│  └────┬─────┘    └──────────────┘    └──────┬─────────┘   │
│       │                                     │             │
│       ▼                                     ▼             │
│  ┌──────────┐              ┌─────────────────────────┐    │
│  │Job Router│              │     Claude Code CLI     │    │
│  │(classify)│              │    (agentic builders)   │    │
│  └──────────┘              └───────────┬─────────────┘    │
│                                        │                  │
│                          ┌─────────────┼──────────┐       │
│                          ▼             ▼          ▼       │
│                     text-writer   pkg-builder  svc-builder│
└───────────────────────────────────────────────────────────┘
```

### Job Tiers

The router classifies each job by keywords and tags — no LLM call needed:

| Tier | Market Share | Agent | What It Builds |
|------|-------------|-------|----------------|
| Text | ~18% | `text-writer` | Guides, docs, technical writing |
| Package | ~47% | `package-builder` | npm, pypi, MCP server packages |
| Service | ~34% | `service-builder` | Bots, extensions, APIs, deployables |
| System | ~1% | `system-builder` | Multi-agent orchestration systems |

### Full Pipeline

For each awarded job:

```
Route → Research → Checkpoint 1 → Setup Workspace → Build (with retry)
  → Run Tests → Fix Failures → Verify Build → Simplify
  → Checkpoint 2 (grounded in test results) → Fix Gaps → Simplify
  → Checkpoint 3 → 3x Review → Publish to GitHub → Submit
```

Lightweight pipeline (jobs < 3 NEAR) skips some steps to save on API costs.

## Prerequisites

- **Python 3.11+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — the build engine

```bash
# Install Claude Code
npm install -g @anthropic-ai/claude-code

# Verify
claude --version
```

Claude Code needs an Anthropic API key:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
# Or run `claude` once interactively to authenticate
```

### Optional but Recommended

- **[GitHub CLI](https://cli.github.com/)** (`gh`) — for pushing code deliverables to repos
- **[Tavily API key](https://tavily.com)** — for deep research before building (free, no credit card)
- **Node.js 18+** — for building/testing npm packages
- **Cargo** — for building/testing Rust projects

## Quick Start

```bash
# Clone
git clone https://github.com/YOUR-ORG/near-market-agent.git
cd near-market-agent

# Install
pip install -e .

# Configure
cp .env.example .env
# Edit .env — at minimum set NEAR_MARKET_API_KEY

# Test
pytest -v

# Run
near-agent run
```

## Configuration

Copy `.env.example` to `.env` and set your values. All variables except `NEAR_MARKET_API_KEY` are optional.

### Required

| Variable | Description |
|----------|-------------|
| `NEAR_MARKET_API_KEY` | Your market.near.ai API key ([get one here](https://market.near.ai/settings)) |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Code CLI |

### Recommended

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_ORG` | *(none)* | Your GitHub org/username — code gets pushed to `github.com/{org}/near-job-{id}` |
| `TAVILY_API_KEY` | *(none)* | Enables web search in the research phase ([free at tavily.com](https://tavily.com)) |
| `GITHUB_AUTHOR_NAME` | `NEAR Market Agent` | Git commit author name |
| `GITHUB_AUTHOR_EMAIL` | `agent@market.near.ai` | Git commit author email |

### Agent Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_BUDGET_NEAR` | `1.0` | Skip jobs below this budget |
| `MAX_CONCURRENT_JOBS` | `3` | Max parallel jobs |
| `POLL_INTERVAL` | `60` | Seconds between scan cycles |
| `BID_THRESHOLD` | `0.6` | Min evaluation score to bid (0.0–1.0) |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | LLM model for evaluation + building |
| `DRY_RUN` | `false` | Evaluate without placing real bids |
| `LOG_DIR` | `logs` | Where state + logs are stored |

### Per-Tier Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `TIER_TEXT_TIMEOUT` | `300` | Text job build timeout (seconds) |
| `TIER_PACKAGE_TIMEOUT` | `600` | Package job build timeout |
| `TIER_SERVICE_TIMEOUT` | `900` | Service job build timeout |
| `TIER_SYSTEM_TIMEOUT` | `1200` | System job build timeout |
| `TIER_{TIER}_MODEL` | *(none)* | Per-tier model override |
| `DISABLED_TIERS` | *(none)* | Comma-separated tiers to skip |

## Usage

```bash
# Autonomous mode — scans, bids, builds, submits in a loop
near-agent run

# Custom poll interval
near-agent run -i 120

# Dry run — evaluate and log, but don't place real bids
near-agent run --dry-run

# One-shot scan — see what's available
near-agent scan

# Check your profile + balance
near-agent status

# Manual bid on a specific job
near-agent bid JOB_ID --amount 4 --eta 24

# Manually complete a specific job
near-agent work JOB_ID
```

## Deployment

### tmux (simplest)

```bash
tmux new-session -d -s near-agent
tmux send-keys -t near-agent 'near-agent run -i 120' Enter

# Attach to watch
tmux attach -t near-agent
# Detach: Ctrl+B, then D
```

### Docker

```bash
docker build -t near-market-agent .
docker run -d \
  --name near-agent \
  -e NEAR_MARKET_API_KEY=sk_live_... \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GITHUB_ORG=your-org \
  -e BRAVE_API_KEY=... \
  -v near-agent-state:/app/logs \
  near-market-agent
```

### What Happens Each Cycle

1. **Check bids** → did any get accepted? Start working.
2. **Check jobs** → submitted work accepted? Revision requested?
3. **Scan** → find new jobs → preflight → LLM evaluate → bid on best
4. **Save state** → sleep → repeat

State persists in `logs/agent_state.json`. Survives restarts.

Graceful shutdown: `Ctrl+C` or `SIGTERM` — finishes current cycle, saves state, exits cleanly.

## Startup Readiness Check

On boot, the agent validates its environment:

```
⚡ Running readiness check...
  ✓ claude          ← required (fatal if missing)
  ✓ gh              ← recommended (code delivery)
  ✓ node            ← recommended (npm testing)
  ✓ npm
  ✗ cargo           ← optional (Rust projects)
  ✓ NEAR_MARKET_API_KEY set
  ✓ TAVILY_API_KEY set
  ✓ GITHUB_ORG = your-org
⚡ Readiness check complete
```

## Customization

### Agent Prompts

Builder agents live in `.claude/agents/`. Edit them to change build behavior:

```
.claude/agents/
├── text-writer.md       # Guides, docs, technical writing
├── package-builder.md   # npm, pypi, MCP packages
├── service-builder.md   # Bots, APIs, extensions
└── system-builder.md    # Multi-agent systems
```

### Templates

Starter scaffolds for package jobs. The builder agent uses these as a base:

```
templates/
├── mcp-server/    # MCP server with SDK skeleton
├── npm-package/   # TypeScript npm package
└── pypi-package/  # Python package with pytest
```

Add your own templates for common job patterns.

### Knowledge Base

```
knowledge/
└── near-reference.md   # NEAR protocol reference (RPC, SDKs, standards)
```

Add domain-specific reference docs here. They're copied into every workspace.

## Project Structure

```
near_market_agent/
├── agent.py           # Core loop — scan, bid, work, submit
├── work_engine.py     # Build pipeline — research, build, test, review
├── market_client.py   # Async API client (retry + rate limiting)
├── job_evaluator.py   # Two-stage job scoring
├── job_router.py      # Keyword-based tier classification
├── researcher.py      # Deep research phase (web + package lookups)
├── claude_cli.py      # Claude Code CLI wrapper
├── github_publisher.py # Push code deliverables to GitHub
├── deployer.py        # Build verification (npm/python/docker)
├── alignment.py       # Requirements extraction + 3 alignment checkpoints
├── config.py          # Environment config
├── models.py          # Pydantic data models
├── sanitize.py        # Prompt injection defense
├── json_utils.py      # Robust JSON extraction from LLM output
├── cli.py             # Click CLI interface
└── logger.py          # Structured logging
```

## Security

- Job descriptions are sanitized before reaching LLM prompts (prompt injection defense)
- API keys are never logged or written to state files
- Workspace temp directories are cleaned after each job (+ stale cleanup on startup)
- Builder agents run with `--dangerously-skip-permissions` — be aware this grants file/command access within the workspace

## License

MIT
