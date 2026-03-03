"""Structured logging for the NEAR Market Agent."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


console = Console(stderr=True)


class AgentLogger:
    """Dual-output logger: rich console + JSON log files."""

    def __init__(self, log_dir: str = "logs", verbose: bool = False):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._log_file = self.log_dir / f"agent_{self._session_id}.jsonl"

    def _write_log(self, level: str, event: str, **data):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event": event,
            **data,
        }
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def info(self, msg: str, **data):
        self._write_log("info", msg, **data)
        if self.verbose:
            console.print(f"[dim]{_ts()}[/] [blue]INFO[/]  {msg}", highlight=False)

    def action(self, msg: str, **data):
        """Log an agent action (bid, submit, etc.)."""
        self._write_log("action", msg, **data)
        console.print(f"[dim]{_ts()}[/] [green]⚡[/]    {msg}", highlight=False)

    def decision(self, msg: str, **data):
        """Log an agent decision."""
        self._write_log("decision", msg, **data)
        console.print(f"[dim]{_ts()}[/] [yellow]🤔[/]    {msg}", highlight=False)

    def warn(self, msg: str, **data):
        self._write_log("warn", msg, **data)
        console.print(f"[dim]{_ts()}[/] [yellow]WARN[/]  {msg}", highlight=False)

    def error(self, msg: str, **data):
        self._write_log("error", msg, **data)
        console.print(f"[dim]{_ts()}[/] [red]ERROR[/] {msg}", highlight=False)

    def scan_results(self, jobs: list, evaluated: list):
        """Display scan results as a rich table."""
        table = Table(title="Job Scan Results", show_lines=True)
        table.add_column("Score", width=6, justify="center")
        table.add_column("Budget", width=8, justify="right")
        table.add_column("Bids", width=5, justify="center")
        table.add_column("Category", width=10)
        table.add_column("Title", max_width=50)
        table.add_column("Bid?", width=5, justify="center")

        for ev in sorted(evaluated, key=lambda e: e.score, reverse=True):
            score_color = "green" if ev.score >= 0.6 else "yellow" if ev.score >= 0.3 else "red"
            bid_icon = "✅" if ev.should_bid else "❌"
            # Find matching job for budget/bids
            job = next((j for j in jobs if j.job_id == ev.job_id), None)
            budget = f"{job.budget_near:.1f}" if job else "?"
            bids = str(job.bid_count or 0) if job else "?"

            table.add_row(
                f"[{score_color}]{ev.score:.2f}[/]",
                f"{budget} NEAR",
                bids,
                ev.category or "—",
                ev.reasoning[:50] if not ev.should_bid else job.title[:50] if job else "?",
                bid_icon,
            )

        console.print(table)

    def job_panel(self, title: str, content: str, style: str = "blue"):
        console.print(Panel(content, title=title, border_style=style))


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")
