"""Core autonomous agent loop."""

from __future__ import annotations

import asyncio
import json
import signal
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .logger import AgentLogger
from .market_client import MarketClient, MarketAPIError
from .job_evaluator import JobEvaluator
from .work_engine import WorkEngine, cleanup_stale_workspaces
from .models import Job, Bid, BidStatus, JobEvaluation


class MarketAgent:
    """Autonomous agent that finds, bids on, and completes jobs on market.near.ai."""

    # Max seen jobs before eviction (keeps oldest out)
    MAX_SEEN_JOBS = 10_000

    def __init__(self, config: Config):
        self.config = config
        self.log = AgentLogger(log_dir=config.log_dir, verbose=config.verbose)
        self.client = MarketClient(config)
        self.evaluator = JobEvaluator(config)
        self.engine = WorkEngine(config)

        # State tracking — OrderedDict preserves insertion order for eviction
        self._seen_jobs: OrderedDict[str, bool] = OrderedDict()
        self._bid_jobs: set[str] = set()
        self._active_bids: dict[str, Bid] = {}
        self._active_jobs: dict[str, Job] = {}
        self._completed: set[str] = set()
        self._revised_assignments: set[str] = set()
        self._state_file = Path(config.log_dir) / "agent_state.json"
        self._running = False
        self._agent_id: str | None = None

    async def run(self):
        """Main agent loop."""
        self.log.action("Agent starting up")
        self._load_state()
        self._running = True

        # Clean up stale workspaces from previous crashes/OOM kills
        cleaned = cleanup_stale_workspaces(max_age_hours=24)
        if cleaned:
            self.log.action(f"Cleaned {cleaned} stale workspace(s) from /tmp")

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except (NotImplementedError, RuntimeError):
                pass

        async with self.client:
            await self._check_identity()

            cycle = 0
            while self._running:
                cycle += 1
                self.log.info(f"-- Cycle {cycle} --")

                try:
                    await self._check_active_bids()
                    await self._check_active_jobs()
                    await self._process_pending_revisions()

                    if len(self._active_jobs) < self.config.max_concurrent_jobs:
                        await self._scan_and_bid()

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

            self._save_state()
            self.log.action("Agent shut down gracefully")

    def _shutdown(self):
        self.log.action("Shutdown signal received, finishing current cycle...")
        self._running = False

    async def scan(self) -> tuple[list[Job], list[JobEvaluation]]:
        """One-shot scan: find and assess jobs without bidding."""
        async with self.client:
            try:
                jobs = await self._fetch_open_jobs()
                self.log.info(f"Found {len(jobs)} open jobs")

                assessments = await self.evaluator.batch_evaluate_async(jobs)
                self.log.scan_results(jobs, assessments)

                bidworthy = [e for e in assessments if e.should_bid]
                self.log.info(f"Assessment complete: {len(bidworthy)}/{len(assessments)} worth bidding on")
                return jobs, assessments
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
        try:
            profile = await self.client.get_profile()
            self._agent_id = profile.agent_id
            balance = await self.client.get_balance()
            self.log.action(f"Authenticated as {profile.handle} (balance: {balance.balance} NEAR)")
        except MarketAPIError as e:
            self.log.error(f"Authentication failed: {e}")
            raise SystemExit(1)

    async def _fetch_open_jobs(self) -> list[Job]:
        """Fetch ALL open standard jobs with full pagination (no cap)."""
        all_jobs: list[Job] = []
        offset = 0
        max_pages = 50  # Safety cap: 50 * 100 = 5000 jobs max

        while True:
            batch = await self.client.list_jobs(
                status="open", job_type="standard",
                sort="budget_amount", order="desc",
                limit=100, offset=offset,
            )
            if not batch:
                break
            all_jobs.extend(batch)
            if len(batch) < 100:
                break  # Last page
            offset += 100
            max_pages -= 1
            if max_pages <= 0:
                self.log.warn(f"Hit safety pagination cap at {len(all_jobs)} jobs")
                break
            await asyncio.sleep(0.5)

        return all_jobs

    async def _scan_and_bid(self):
        """Find new jobs, assess them, and bid on the best ones."""
        jobs = await self._fetch_open_jobs()
        new_jobs = [j for j in jobs if j.job_id not in self._seen_jobs]

        if not new_jobs:
            self.log.info("No new jobs found")
            return

        self.log.info(f"Assessing {len(new_jobs)} new jobs")
        assessments = await self.evaluator.batch_evaluate_async(new_jobs)

        # Mark all as seen with ordered eviction (FIFO)
        for j in new_jobs:
            self._seen_jobs[j.job_id] = True
        self._evict_seen_jobs()

        # Bid on worthy jobs (must pass both LLM should_bid AND confidence threshold)
        bidworthy = sorted(
            [e for e in assessments
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

        self.log.scan_results(new_jobs, assessments)

        for ev in to_bid:
            job = next((j for j in new_jobs if j.job_id == ev.job_id), None)
            if job is None:
                self.log.warn(f"Assessed job {ev.job_id} vanished from job list, skipping bid")
                continue
            await self._place_bid(job, ev)

    def _evict_seen_jobs(self):
        """Evict oldest entries when seen_jobs exceeds MAX_SEEN_JOBS."""
        while len(self._seen_jobs) > self.MAX_SEEN_JOBS:
            self._seen_jobs.popitem(last=False)  # Remove oldest (FIFO)

    async def _place_bid(self, job: Job, assessment: JobEvaluation):
        amount = str(assessment.suggested_bid_amount or job.budget_near)
        eta = (assessment.suggested_eta_hours or 24) * 3600
        proposal = assessment.proposal_draft

        if not proposal or not proposal.strip():
            self.log.warn(f"Empty proposal for {job.title[:40]}, generating fallback", job_id=job.job_id)
            proposal = (
                f"I can complete this job efficiently. My capabilities include "
                f"{', '.join(self.config.capabilities.skills[:5])}. "
                f"Estimated delivery: {eta // 3600} hours."
            )

        if self.config.dry_run:
            self.log.decision(
                f"[DRY RUN] Would bid {amount} NEAR on: {job.title[:60]}",
                job_id=job.job_id, amount=amount, eta_hours=eta // 3600,
            )
            return

        try:
            bid = await self.client.place_bid(
                job_id=job.job_id, amount=amount,
                eta_seconds=eta, proposal=proposal,
            )
            self._active_bids[bid.bid_id] = bid
            self._bid_jobs.add(job.job_id)
            self.log.action(
                f"Bid placed: {amount} NEAR on \"{job.title[:50]}\"",
                job_id=job.job_id, bid_id=bid.bid_id, amount=amount,
            )
        except MarketAPIError as e:
            self.log.error(f"Failed to bid on {job.title[:40]}: {e}", job_id=job.job_id)

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
        work_queue: list[tuple[Job, str]] = []

        for bid_id, bid in list(self._active_bids.items()):
            try:
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
                                f"Bid ACCEPTED! Starting work on: {job.title[:50]}",
                                job_id=job.job_id, bid_id=bid_id,
                            )
                            self._active_jobs[job.job_id] = job
                            to_remove.append(bid_id)
                            work_queue.append((job, assignment_id))
                            break

                if job.status.value not in ("open", "filling"):
                    if job.job_id not in self._active_jobs:
                        self.log.info(
                            f"Bid on \"{job.title[:40]}\" -- job no longer open ({job.status.value})",
                            job_id=job.job_id,
                        )
                        to_remove.append(bid_id)

            except MarketAPIError as e:
                self.log.warn(f"Failed to check bid {bid_id}: {e}", bid_id=bid_id)

        for bid_id in to_remove:
            self._active_bids.pop(bid_id, None)

        for job, assignment_id in work_queue:
            await self._do_work(job, assignment_id)

    async def _check_active_jobs(self):
        """Check on jobs we're currently working on and detect revision requests."""
        to_remove: list[str] = []
        for job_id, job in list(self._active_jobs.items()):
            try:
                updated = await self.client.get_job(job_id)
                if updated.my_assignments:
                    for asn in updated.my_assignments:
                        status = asn.get("status", "")
                        assignment_id = asn.get("assignment_id", "")

                        if status == "accepted":
                            self.log.action(f"Work ACCEPTED on: {updated.title[:50]}", job_id=job_id)
                            self._completed.add(job_id)
                            to_remove.append(job_id)
                        elif status == "submitted":
                            self.log.info(f"Waiting for review: {updated.title[:50]}", job_id=job_id)
                        elif status == "in_progress" and asn.get("deliverable"):
                            # Revision requested — queue it
                            if assignment_id and assignment_id not in self._revised_assignments:
                                self._queue_revision(updated, assignment_id, asn)
                        elif status == "disputed":
                            self.log.warn(f"Work DISPUTED on: {updated.title[:50]}", job_id=job_id)
                        elif status == "cancelled":
                            self.log.warn(f"Assignment CANCELLED: {updated.title[:50]}", job_id=job_id)
                            to_remove.append(job_id)
                elif updated.status.value in ("closed", "expired", "completed"):
                    self.log.warn(
                        f"Job ended without assignment update ({updated.status.value}): {updated.title[:50]}",
                        job_id=job_id,
                    )
                    to_remove.append(job_id)
            except MarketAPIError as e:
                self.log.warn(f"Failed to check job {job_id}: {e}", job_id=job_id)

        for job_id in to_remove:
            self._active_jobs.pop(job_id, None)

    def _queue_revision(self, job: Job, assignment_id: str, assignment: dict):
        """Queue a revision request for processing."""
        self.log.action(
            f"Revision requested for: {job.title[:50]}",
            job_id=job.job_id, assignment_id=assignment_id,
        )
        if not hasattr(self, '_pending_revisions'):
            self._pending_revisions: list[tuple[Job, str, str]] = []
        self._pending_revisions.append(
            (job, assignment_id, assignment.get("deliverable", ""))
        )

    async def _process_pending_revisions(self):
        """Process any pending revision requests."""
        if not hasattr(self, '_pending_revisions') or not self._pending_revisions:
            return

        revisions = self._pending_revisions[:]
        self._pending_revisions.clear()

        for job, assignment_id, original_deliverable in revisions:
            if assignment_id in self._revised_assignments:
                continue

            # Fetch feedback from assignment messages
            feedback = await self._fetch_revision_feedback(job, assignment_id)
            if not feedback:
                feedback = "The requester has requested revisions. Please improve the deliverable."

            self._revised_assignments.add(assignment_id)
            await self._do_revision(job, assignment_id, original_deliverable, feedback)

    async def _fetch_revision_feedback(self, job: Job, assignment_id: str) -> str:
        """Fetch revision feedback from assignment messages."""
        try:
            messages = await self.client.get_assignment_messages(assignment_id, limit=10)
            # Get the most recent non-agent message (from the requester)
            for msg in reversed(messages):
                if msg.sender_agent_id != self._agent_id:
                    return msg.content
        except MarketAPIError as e:
            self.log.warn(f"Failed to fetch revision feedback: {e}", job_id=job.job_id)
        return ""

    async def _do_work(self, job: Job, assignment_id: str):
        """Complete a job and submit the deliverable."""
        self.log.action(f"Working on: {job.title[:50]}", job_id=job.job_id, assignment_id=assignment_id)

        if self.config.dry_run:
            self.log.decision(f"[DRY RUN] Would complete and submit work for: {job.title[:50]}", job_id=job.job_id)
            return

        try:
            result = await self.engine.complete_job_async(job)
            self.log.info(
                f"Work complete ({result.tokens_used} tokens, {len(result.content)} chars)",
                job_id=job.job_id,
            )
            await self._save_and_submit(job, result, "deliverable")
        except Exception as e:
            self.log.error(f"Failed to complete/submit work: {e}", job_id=job.job_id)

    async def _do_revision(self, job: Job, assignment_id: str, original: str, feedback: str):
        """Handle a revision request — revise and resubmit."""
        self.log.action(f"Revising: {job.title[:50]}", job_id=job.job_id, feedback=feedback[:200])

        if self.config.dry_run:
            self.log.decision(f"[DRY RUN] Would revise and resubmit for: {job.title[:50]}", job_id=job.job_id)
            return

        try:
            result = await self.engine.handle_revision_async(job, original, feedback)
            self.log.info(
                f"Revision complete ({result.tokens_used} tokens, {len(result.content)} chars)",
                job_id=job.job_id,
            )
            await self._save_and_submit(job, result, "revision")
        except Exception as e:
            self.log.error(f"Failed to revise: {e}", job_id=job.job_id)

    async def _save_and_submit(self, job: Job, result, label: str):
        """Save deliverable locally and submit to market."""
        filepath = Path(self.config.log_dir) / f"{label}_{job.job_id[:8]}.md"
        filepath.write_text(result.content, encoding="utf-8")
        self.log.info(f"Saved {label} to {filepath}", job_id=job.job_id)

        await self.client.submit_deliverable(
            job_id=job.job_id,
            deliverable=result.content,
            deliverable_hash=result.content_hash,
        )
        self.log.action(f"Submitted {label} for: {job.title[:50]}", job_id=job.job_id)

    # --- State persistence ---

    def _save_state(self):
        state = {
            "seen_jobs": list(self._seen_jobs.keys()),
            "bid_jobs": list(self._bid_jobs),
            "active_bids": {k: v.model_dump(mode="json") for k, v in self._active_bids.items()},
            "active_jobs": list(self._active_jobs.keys()),
            "completed": list(self._completed),
            "revised_assignments": list(self._revised_assignments),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_path.replace(self._state_file)

    def _load_state(self):
        if not self._state_file.exists():
            return
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
            # Restore seen_jobs as OrderedDict (preserves insertion order)
            self._seen_jobs = OrderedDict(
                (jid, True) for jid in state.get("seen_jobs", [])
            )
            self._bid_jobs = set(state.get("bid_jobs", []))
            self._completed = set(state.get("completed", []))
            self._revised_assignments = set(state.get("revised_assignments", []))
            raw_bids = state.get("active_bids", {})
            if isinstance(raw_bids, dict):
                self._active_bids = {}
                for bid_id, bid_data in raw_bids.items():
                    try:
                        self._active_bids[bid_id] = Bid.model_validate(bid_data)
                    except Exception:
                        continue
            self.log.info(
                f"Restored state: {len(self._seen_jobs)} seen, "
                f"{len(self._active_bids)} active bids, "
                f"{len(self._completed)} completed"
            )
        except (json.JSONDecodeError, KeyError):
            self.log.warn("Failed to load state, starting fresh")
