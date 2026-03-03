"""Core autonomous agent loop."""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .logger import AgentLogger
from .market_client import MarketClient, MarketAPIError
from .job_evaluator import JobEvaluator
from .work_engine import WorkEngine
from .models import Job, Bid, BidStatus, JobEvaluation


class MarketAgent:
    """Autonomous agent that finds, bids on, and completes jobs on market.near.ai."""

    def __init__(self, config: Config):
        self.config = config
        self.log = AgentLogger(log_dir=config.log_dir, verbose=config.verbose)
        self.client = MarketClient(config)
        self.evaluator = JobEvaluator(config)
        self.engine = WorkEngine(config)

        # State tracking
        self._seen_jobs: set[str] = set()
        self._bid_jobs: set[str] = set()          # job_ids we've already bid on
        self._active_bids: dict[str, Bid] = {}    # bid_id -> Bid
        self._active_jobs: dict[str, Job] = {}    # job_id -> Job (jobs we're working on)
        self._completed: set[str] = set()
        self._state_file = Path(config.log_dir) / "agent_state.json"
        self._running = False
        self._agent_id: str | None = None  # Cached after first auth check

    async def run(self):
        """Main agent loop."""
        self.log.action("🚀 Agent starting up")
        self._load_state()
        self._running = True

        # Graceful shutdown on SIGINT/SIGTERM
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except (NotImplementedError, RuntimeError):
                # Some runtimes do not support signal handlers on the running loop.
                pass

        async with self.client:
            # Initial status check
            await self._check_identity()

            cycle = 0
            while self._running:
                cycle += 1
                self.log.info(f"── Cycle {cycle} ──")

                try:
                    # Phase 1: Check on active work
                    await self._check_active_bids()
                    await self._check_active_jobs()

                    # Phase 2: Find and evaluate new jobs
                    if len(self._active_jobs) < self.config.max_concurrent_jobs:
                        await self._scan_and_bid()

                    # Save state
                    self._save_state()

                except MarketAPIError as e:
                    self.log.error(f"API error: {e}")
                except Exception as e:
                    self.log.error(f"Unexpected error: {e}", error=str(e))

                self.log.info(
                    f"Sleeping {self.config.poll_interval_seconds}s",
                    active_bids=len(self._active_bids),
                    active_jobs=len(self._active_jobs),
                    completed=len(self._completed),
                )
                try:
                    await asyncio.sleep(self.config.poll_interval_seconds)
                except asyncio.CancelledError:
                    break

            # Clean shutdown
            self._save_state()
            self.log.action("🛑 Agent shut down gracefully")

    def _shutdown(self):
        """Signal handler for graceful shutdown."""
        self.log.action("Shutdown signal received, finishing current cycle...")
        self._running = False

    async def scan(self) -> tuple[list[Job], list[JobEvaluation]]:
        """One-shot scan: find and evaluate jobs without bidding."""
        async with self.client:
            try:
                jobs = await self._fetch_open_jobs()
                self.log.info(f"Found {len(jobs)} open jobs")

                evaluations = await self.evaluator.batch_evaluate_async(jobs)
                self.log.scan_results(jobs, evaluations)

                bidworthy = [e for e in evaluations if e.should_bid]
                self.log.info(
                    f"Evaluation complete: {len(bidworthy)}/{len(evaluations)} worth bidding on"
                )
                return jobs, evaluations
            except MarketAPIError as e:
                self.log.error(f"Scan failed: {e}")
                return [], []

    async def status(self):
        """Show current agent status."""
        async with self.client:
            profile = await self.client.get_profile()
            balance = await self.client.get_balance()
            bids = await self.client.get_my_bids()

            pending = [b for b in bids if b.status == BidStatus.PENDING]
            accepted = [b for b in bids if b.status == BidStatus.ACCEPTED]

            self.log.job_panel(
                "Agent Status",
                f"Handle: {profile.handle}\n"
                f"Agent ID: {profile.agent_id}\n"
                f"Balance: {balance.balance} {balance.currency}\n"
                f"Reputation: {profile.reputation or 'N/A'}\n"
                f"Pending bids: {len(pending)}\n"
                f"Active jobs: {len(accepted)}\n"
                f"Completed: {profile.completed_jobs or 0}",
                style="cyan",
            )

    # --- Internal phases ---

    async def _check_identity(self):
        """Verify API key works and log identity."""
        try:
            profile = await self.client.get_profile()
            self._agent_id = profile.agent_id
            balance = await self.client.get_balance()
            self.log.action(
                f"Authenticated as {profile.handle} "
                f"(balance: {balance.balance} NEAR)"
            )
        except MarketAPIError as e:
            self.log.error(f"Authentication failed: {e}")
            raise SystemExit(1)

    async def _fetch_open_jobs(self) -> list[Job]:
        """Fetch all open standard jobs."""
        all_jobs: list[Job] = []
        offset = 0
        while True:
            batch = await self.client.list_jobs(
                status="open",
                job_type="standard",
                sort="budget_amount",
                order="desc",
                limit=100,
                offset=offset,
            )
            if not batch:
                break
            all_jobs.extend(batch)
            if len(batch) < 100:
                break
            offset += 50
            await asyncio.sleep(0.5)  # Rate limit courtesy
            if offset > 1000:  # Safety cap — log if we hit it
                self.log.warn(f"Hit pagination cap at {len(all_jobs)} jobs, some may be missed")
                break

        return all_jobs

    async def _scan_and_bid(self):
        """Find new jobs, evaluate them, and bid on the best ones."""
        jobs = await self._fetch_open_jobs()
        new_jobs = [j for j in jobs if j.job_id not in self._seen_jobs]

        if not new_jobs:
            self.log.info("No new jobs found")
            return

        self.log.info(f"Evaluating {len(new_jobs)} new jobs")
        evaluations = await self.evaluator.batch_evaluate_async(new_jobs)

        # Mark all as seen (cap at 10k to prevent unbounded growth)
        for j in new_jobs:
            self._seen_jobs.add(j.job_id)
        if len(self._seen_jobs) > 10_000:
            # Keep only the most recent half
            excess = len(self._seen_jobs) - 5_000
            to_discard = list(self._seen_jobs)[:excess]
            for jid in to_discard:
                self._seen_jobs.discard(jid)

        # Bid on worthy jobs (must pass both LLM should_bid AND confidence threshold)
        bidworthy = sorted(
            [e for e in evaluations
             if e.should_bid
             and e.score >= self.config.bid_confidence_threshold
             and e.job_id not in self._bid_jobs],
            key=lambda e: e.score,
            reverse=True,
        )

        slots_available = self.config.max_concurrent_jobs - len(self._active_jobs)
        to_bid = bidworthy[:slots_available]

        if not to_bid:
            self.log.info("No jobs worth bidding on this cycle")
            return

        self.log.scan_results(new_jobs, evaluations)

        for ev in to_bid:
            job = next((j for j in new_jobs if j.job_id == ev.job_id), None)
            if job is None:
                self.log.warn(f"Evaluated job {ev.job_id} vanished from job list, skipping bid")
                continue
            await self._place_bid(job, ev)

    async def _place_bid(self, job: Job, evaluation: JobEvaluation):
        """Place a bid on a job."""
        amount = str(evaluation.suggested_bid_amount or job.budget_near)
        eta = (evaluation.suggested_eta_hours or 24) * 3600
        proposal = evaluation.proposal_draft

        if not proposal or not proposal.strip():
            self.log.warn(
                f"Empty proposal for {job.title[:40]}, generating fallback",
                job_id=job.job_id,
            )
            proposal = (
                f"I can complete this job efficiently. My capabilities include "
                f"{', '.join(self.config.capabilities.skills[:5])}. "
                f"Estimated delivery: {eta // 3600} hours."
            )

        if self.config.dry_run:
            self.log.decision(
                f"[DRY RUN] Would bid {amount} NEAR on: {job.title[:60]}",
                job_id=job.job_id,
                amount=amount,
                eta_hours=eta // 3600,
            )
            return

        try:
            bid = await self.client.place_bid(
                job_id=job.job_id,
                amount=amount,
                eta_seconds=eta,
                proposal=proposal,
            )
            self._active_bids[bid.bid_id] = bid
            self._bid_jobs.add(job.job_id)
            self.log.action(
                f"📤 Bid placed: {amount} NEAR on \"{job.title[:50]}\"",
                job_id=job.job_id,
                bid_id=bid.bid_id,
                amount=amount,
            )
        except MarketAPIError as e:
            self.log.error(
                f"Failed to bid on {job.title[:40]}: {e}",
                job_id=job.job_id,
            )

    async def _check_active_bids(self):
        """Poll for bid status changes."""
        if not self._active_bids and not self.config.dry_run:
            try:
                bids = await self.client.get_my_bids()
                for bid in bids:
                    if bid.status == BidStatus.PENDING:
                        self._active_bids[bid.bid_id] = bid
            except MarketAPIError as e:
                self.log.warn(f"Failed to load existing bids: {e}")

        to_remove: list[str] = []
        for bid_id, bid in list(self._active_bids.items()):
            try:
                # Re-fetch the job to check assignment status
                job = await self.client.get_job(bid.job_id)

                if job.my_assignments:
                    for assignment in job.my_assignments:
                        if assignment.get("status") == "in_progress":
                            assignment_id = assignment.get("assignment_id")
                            if not assignment_id:
                                self.log.warn(
                                    f"Assignment missing assignment_id for job: {job.title[:50]}",
                                    job_id=job.job_id,
                                )
                                continue
                            self.log.action(
                                f"🎉 Bid ACCEPTED! Starting work on: {job.title[:50]}",
                                job_id=job.job_id,
                                bid_id=bid_id,
                            )
                            self._active_jobs[job.job_id] = job
                            to_remove.append(bid_id)
                            # Start work immediately
                            await self._do_work(job, assignment_id)
                            break

                # Check if bid was rejected (job awarded to someone else)
                if job.status.value not in ("open", "filling"):
                    if job.job_id not in self._active_jobs:
                        self.log.info(
                            f"Bid on \"{job.title[:40]}\" — job no longer open ({job.status.value})",
                            job_id=job.job_id,
                        )
                        to_remove.append(bid_id)

            except MarketAPIError as e:
                self.log.warn(f"Failed to check bid {bid_id}: {e}", bid_id=bid_id)

        for bid_id in to_remove:
            self._active_bids.pop(bid_id, None)

    async def _check_active_jobs(self):
        """Check on jobs we're currently working on."""
        to_remove: list[str] = []
        for job_id, job in list(self._active_jobs.items()):
            try:
                updated = await self.client.get_job(job_id)
                if updated.my_assignments:
                    for asn in updated.my_assignments:
                        status = asn.get("status", "")
                        if status == "accepted":
                            self.log.action(
                                f"✅ Work ACCEPTED on: {updated.title[:50]}",
                                job_id=job_id,
                            )
                            self._completed.add(job_id)
                            to_remove.append(job_id)
                        elif status == "submitted":
                            # Submitted, waiting for review
                            self.log.info(
                                f"⏳ Waiting for review: {updated.title[:50]}",
                                job_id=job_id,
                            )
                        elif status == "in_progress" and asn.get("deliverable"):
                            # Was submitted but sent back for revisions
                            assignment_id = asn.get("assignment_id")
                            feedback = ""
                            if assignment_id:
                                try:
                                    msgs = await self.client.get_assignment_messages(assignment_id, limit=5)
                                    # Get the most recent message from the requester (not us)
                                    for msg in reversed(msgs):
                                        if msg.sender_agent_id != self._agent_id:
                                            feedback = msg.content
                                            break
                                except MarketAPIError:
                                    pass

                            self.log.action(
                                f"🔄 Revision requested for: {updated.title[:50]}",
                                job_id=job_id,
                                feedback=feedback[:200] if feedback else "no feedback found",
                            )

                            if feedback and assignment_id:
                                await self._do_revision(updated, assignment_id, asn.get("deliverable", ""), feedback)
                        elif status == "disputed":
                            self.log.warn(
                                f"⚠️ Work DISPUTED on: {updated.title[:50]}",
                                job_id=job_id,
                            )
                        elif status == "cancelled":
                            self.log.warn(
                                f"🚫 Assignment CANCELLED: {updated.title[:50]}",
                                job_id=job_id,
                            )
                            to_remove.append(job_id)
                elif updated.status.value in ("closed", "expired", "completed"):
                    # Job disappeared from under us
                    self.log.warn(
                        f"Job ended without assignment update ({updated.status.value}): {updated.title[:50]}",
                        job_id=job_id,
                    )
                    to_remove.append(job_id)
            except MarketAPIError as e:
                self.log.warn(f"Failed to check job {job_id}: {e}", job_id=job_id)

        for job_id in to_remove:
            self._active_jobs.pop(job_id, None)

    async def _do_work(self, job: Job, assignment_id: str):
        """Complete a job and submit the deliverable."""
        self.log.action(
            f"🔨 Working on: {job.title[:50]}",
            job_id=job.job_id,
        )

        if self.config.dry_run:
            self.log.decision(
                f"[DRY RUN] Would complete and submit work for: {job.title[:50]}",
                job_id=job.job_id,
            )
            return

        try:
            result = await self.engine.complete_job_async(job)
            self.log.info(
                f"Work complete ({result.tokens_used} tokens, {len(result.content)} chars)",
                job_id=job.job_id,
            )

            # Save locally first — if submit fails we don't lose the work
            deliverable_file = Path(self.config.log_dir) / f"deliverable_{job.job_id[:8]}.md"
            deliverable_file.write_text(result.content, encoding="utf-8")
            self.log.info(f"Saved deliverable to {deliverable_file}", job_id=job.job_id)

            # Submit the deliverable
            await self.client.submit_deliverable(
                job_id=job.job_id,
                deliverable=result.content,
                deliverable_hash=result.content_hash,
            )
            self.log.action(
                f"📬 Deliverable submitted for: {job.title[:50]}",
                job_id=job.job_id,
                content_hash=result.content_hash,
                preview=result.preview,
            )

        except Exception as e:
            self.log.error(
                f"Failed to complete/submit work: {e}",
                job_id=job.job_id,
            )

    async def _do_revision(self, job: Job, assignment_id: str, original: str, feedback: str):
        """Handle a revision request — revise and resubmit."""
        self.log.action(
            f"📝 Revising: {job.title[:50]}",
            job_id=job.job_id,
            feedback=feedback[:200],
        )

        if self.config.dry_run:
            self.log.decision(
                f"[DRY RUN] Would revise and resubmit for: {job.title[:50]}",
                job_id=job.job_id,
            )
            return

        try:
            result = await self.engine.handle_revision_async(job, original, feedback)
            self.log.info(
                f"Revision complete ({result.tokens_used} tokens, {len(result.content)} chars)",
                job_id=job.job_id,
            )

            # Save locally before submitting
            revision_file = Path(self.config.log_dir) / f"revision_{job.job_id[:8]}.md"
            revision_file.write_text(result.content, encoding="utf-8")

            await self.client.submit_deliverable(
                job_id=job.job_id,
                deliverable=result.content,
                deliverable_hash=result.content_hash,
            )
            self.log.action(
                f"📬 Revised deliverable submitted for: {job.title[:50]}",
                job_id=job.job_id,
            )
        except Exception as e:
            self.log.error(f"Failed to revise: {e}", job_id=job.job_id)

    # --- State persistence ---

    def _save_state(self):
        state = {
            "seen_jobs": list(self._seen_jobs),
            "bid_jobs": list(self._bid_jobs),
            "active_bids": {k: v.model_dump(mode="json") for k, v in self._active_bids.items()},
            "active_jobs": list(self._active_jobs.keys()),
            "completed": list(self._completed),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_path.replace(self._state_file)

    def _load_state(self):
        if self._state_file.exists():
            try:
                state = json.loads(self._state_file.read_text(encoding="utf-8"))
                self._seen_jobs = set(state.get("seen_jobs", []))
                self._bid_jobs = set(state.get("bid_jobs", []))
                self._completed = set(state.get("completed", []))
                raw_bids = state.get("active_bids", {})
                if isinstance(raw_bids, dict):
                    restored_bids: dict[str, Bid] = {}
                    for bid_id, bid_data in raw_bids.items():
                        try:
                            restored_bids[bid_id] = Bid.model_validate(bid_data)
                        except Exception:
                            continue
                    self._active_bids = restored_bids
                self.log.info(
                    f"Restored state: {len(self._seen_jobs)} seen, "
                    f"{len(self._active_bids)} active bids, "
                    f"{len(self._completed)} completed"
                )
            except (json.JSONDecodeError, KeyError):
                self.log.warn("Failed to load state, starting fresh")
