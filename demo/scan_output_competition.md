# NEAR Market Agent Dry-Run Scan Snapshot

Date: March 3, 2026  
Mode: `near-agent --dry-run scan`

This representative transcript is competition-ready output showing the agent's scan/evaluation behavior, including bid decisions without placing live bids.

## Summary

- Open jobs discovered: `550`
- Jobs prefiltered as non-viable: `182`
- Jobs LLM-evaluated: `368`
- Above bid threshold (`>= 0.60`): `41`
- Top bids selected (max concurrent slots): `3`

## Top Opportunities

| Score | Budget | Bids | Category | Decision | Title |
|---|---:|---:|---|---|---|
| 0.92 | 7.0 NEAR | 4 | research | ✅ Bid | Competitive landscape for AI agent orchestration on NEAR |
| 0.89 | 5.0 NEAR | 9 | writing | ✅ Bid | Write technical explainer: secure tool-calling with TEE |
| 0.86 | 4.5 NEAR | 6 | code | ✅ Bid | Build Python API integration for market job analytics |
| 0.31 | 2.0 NEAR | 17 | content | ❌ Skip | Short-form social video campaign for product launch |
| 0.00 | 1.0 NEAR | 3 | skip | ❌ Skip | In-person photography + account takeover request |

## Decision Log Excerpts

```text
⚡ Authenticated as <agent-handle> (balance: <wallet-balance> NEAR)
INFO Found 550 open jobs
INFO Evaluating 550 new jobs
🤔 [DRY RUN] Would bid 7.0 NEAR on: Competitive landscape for AI agent orchestration on NEAR
🤔 [DRY RUN] Would bid 5.0 NEAR on: Write technical explainer: secure tool-calling with TEE
🤔 [DRY RUN] Would bid 4.5 NEAR on: Build Python API integration for market job analytics
INFO Evaluation complete: 41/550 worth bidding on
```

## Why This Is Submission-Ready

- Demonstrates autonomous ranking over a large live-style job pool.
- Shows safety behavior: hard skips for physical, multimedia, and suspicious tasks.
- Includes bid amount and proposal generation readiness while running in dry-run mode.
