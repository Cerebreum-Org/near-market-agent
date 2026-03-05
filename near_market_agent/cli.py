"""CLI entry point for the NEAR Market Agent."""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console

from .config import Config
from .agent import MarketAgent

console = Console()


@click.group()
@click.option("--dry-run", is_flag=True, help="Evaluate and log but don't place real bids")
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging")
@click.pass_context
def cli(ctx, dry_run: bool, verbose: bool):
    """🤖 NEAR Market Agent — Autonomous job hunter for market.near.ai"""
    config = Config.from_env()
    config.dry_run = dry_run or config.dry_run
    config.verbose = verbose or config.verbose
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@cli.command()
@click.option("--interval", "-i", default=60, type=click.IntRange(min=1), help="Poll interval in seconds")
@click.pass_context
def run(ctx, interval: int):
    """Run the autonomous agent loop."""
    config: Config = ctx.obj["config"]
    config.poll_interval_seconds = interval

    errors = config.validate()
    if errors:
        for e in errors:
            console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    agent = MarketAgent(config)
    console.print("[bold green]🚀 Starting autonomous agent loop[/]")
    if config.dry_run:
        console.print("[yellow]⚠ DRY RUN mode — no real bids will be placed[/]")

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped by user[/]")


@cli.command()
@click.pass_context
def scan(ctx):
    """Scan open jobs and evaluate them (no bidding)."""
    config: Config = ctx.obj["config"]
    config.dry_run = True

    errors = config.validate()
    if errors:
        for e in errors:
            console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    agent = MarketAgent(config)
    asyncio.run(agent.scan())


@cli.command()
@click.pass_context
def status(ctx):
    """Show agent profile, balance, and active work."""
    config: Config = ctx.obj["config"]

    if not config.market_api_key:
        console.print("[red]✗ NEAR_MARKET_API_KEY not set[/]")
        sys.exit(1)

    agent = MarketAgent(config)
    asyncio.run(agent.status())


@cli.command()
@click.argument("job_id")
@click.option("--amount", "-a", required=True, help="Bid amount in NEAR")
@click.option("--eta", "-e", default=24, type=click.IntRange(min=1), help="Estimated hours to complete")
@click.option("--proposal", "-p", help="Proposal text (or auto-generate)")
@click.option("--force", is_flag=True, help="Bypass bid-confidence threshold check")
@click.pass_context
def bid(ctx, job_id: str, amount: str, eta: int, proposal: str | None, force: bool):
    """Place a bid on a specific job."""
    config: Config = ctx.obj["config"]

    errors = config.validate()
    if errors:
        for e in errors:
            console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    async def _bid():
        agent = MarketAgent(config)
        async with agent.client:
            job = await agent.client.get_job(job_id)
            console.print(f"[bold]{job.title}[/]")
            console.print(f"Budget: {job.budget_near} NEAR | Bids: {job.bid_count}")

            try:
                amount_value = float(amount)
            except (TypeError, ValueError):
                raise click.ClickException("Amount must be a valid number")

            if amount_value <= 0:
                raise click.ClickException("Amount must be greater than 0")

            ev = await agent.evaluator.evaluate_job_async(job)
            if ev.score < config.bid_confidence_threshold and not force:
                raise click.ClickException(
                    f"Job score {ev.score:.2f} is below BID_THRESHOLD "
                    f"({config.bid_confidence_threshold:.2f}). Use --force to override."
                )

            if not proposal:
                final_proposal = ev.proposal_draft
                console.print(f"\n[dim]Auto-generated proposal:[/]\n{final_proposal[:500]}")
            else:
                final_proposal = proposal

            if not final_proposal.strip():
                raise click.ClickException("Proposal cannot be empty")

            if config.dry_run:
                console.print(f"\n[yellow]DRY RUN — would bid {amount_value} NEAR[/]")
                return

            bid_result = await agent.client.place_bid(
                job_id=job_id,
                amount=str(amount_value),
                eta_seconds=eta * 3600,
                proposal=final_proposal,
            )
            console.print(f"\n[green]✓ Bid placed![/] ID: {bid_result.bid_id}")

    asyncio.run(_bid())


@cli.command()
@click.argument("job_id")
@click.pass_context
def work(ctx, job_id: str):
    """Complete work for an awarded job and submit deliverable."""
    config: Config = ctx.obj["config"]

    errors = config.validate()
    if errors:
        for e in errors:
            console.print(f"[red]✗[/] {e}")
        sys.exit(1)

    async def _work():
        from pathlib import Path

        agent = MarketAgent(config)
        async with agent.client:
            job = await agent.client.get_job(job_id)
            console.print(f"[bold]Working on: {job.title}[/]")

            result = await agent.engine.complete_job_async(job)
            console.print(f"\n[dim]Produced {len(result.content)} chars ({result.tokens_used} tokens)[/]")
            console.print(f"\n{result.content[:1000]}")

            # Save locally first — safety net in case submit fails
            log_dir = Path(config.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            deliverable_file = log_dir / f"deliverable_{job_id[:8]}.md"
            deliverable_file.write_text(result.content, encoding="utf-8")
            console.print(f"\n[dim]Saved to {deliverable_file}[/]")

            if config.dry_run:
                console.print(f"\n[yellow]DRY RUN — deliverable not submitted[/]")
                return

            await agent.client.submit_deliverable(
                job_id=job_id,
                deliverable=result.content,
                deliverable_hash=result.content_hash,
            )
            console.print(f"\n[green]✓ Deliverable submitted![/]")

    asyncio.run(_work())


@cli.command()
@click.pass_context
def dashboard(ctx):
    """Show performance dashboard — earnings, win rate, insights."""
    from rich.panel import Panel
    from rich.table import Table
    from .learner import Learner

    config: Config = ctx.obj["config"]
    learner = Learner(log_dir=config.log_dir)
    stats = learner.compute_stats()

    # Stats panel
    console.print(Panel(
        f"[bold green]💰 Earned:[/] {stats.total_earned_near:.1f} NEAR\n"
        f"[bold]📊 Win Rate:[/] {stats.win_rate:.0%} ({stats.bids_accepted}/{stats.total_bids} bids)\n"
        f"[bold]✅ Acceptance:[/] {stats.acceptance_rate:.0%} ({stats.jobs_accepted}/{stats.jobs_completed} submitted)\n"
        f"[bold]🔄 Revisions:[/] {stats.total_revisions}\n"
        f"[bold]🔥 Streak:[/] {stats.streak} accepted",
        title="[bold cyan]Agent Performance[/]",
        border_style="cyan",
    ))

    # Bids table
    table = Table(title="Bid Status")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Pending", str(stats.bids_pending))
    table.add_row("[green]Accepted[/]", str(stats.bids_accepted))
    table.add_row("[red]Rejected[/]", str(stats.bids_rejected))
    table.add_row("[yellow]Disputed[/]", str(stats.jobs_disputed))
    console.print(table)

    # Quality metrics
    if stats.avg_review_score > 0:
        console.print(f"\n[bold]Quality Metrics:[/]")
        console.print(f"  Avg Review Score: {stats.avg_review_score:.2f}/1.0")
        console.print(f"  Avg Build Time: {stats.avg_build_time_seconds:.0f}s")
        if stats.best_tier:
            console.print(f"  Best Tier: [green]{stats.best_tier}[/]")
        if stats.worst_tier:
            console.print(f"  Worst Tier: [red]{stats.worst_tier}[/]")

    # Learning insights
    insights = learner.analyze_patterns()
    if insights:
        console.print(f"\n[bold yellow]💡 Learning Insights:[/]")
        for i in insights:
            console.print(f"  [{i.category}] {i.insight}")
            console.print(f"    → [dim]{i.action}[/]")
    elif len(learner._outcomes) < 5:
        console.print(f"\n[dim]Need more data for insights (have {len(learner._outcomes)}, need 5+)[/]")
    else:
        console.print(f"\n[dim]No actionable insights yet[/]")


@cli.command()
@click.pass_context
def insights(ctx):
    """Analyze patterns and show learning insights."""
    from .learner import Learner

    config: Config = ctx.obj["config"]
    learner = Learner(log_dir=config.log_dir)
    results = learner.analyze_patterns()

    if not results:
        console.print("[dim]No insights yet — need more outcome data[/]")
        return

    for i in results:
        emoji = {"pricing": "💰", "quality": "⭐", "tier": "📊", "timing": "⏱"}.get(i.category, "💡")
        console.print(f"\n{emoji} [bold]{i.category.upper()}[/] (confidence: {i.confidence:.0%})")
        console.print(f"  {i.insight}")
        console.print(f"  → [green]{i.action}[/]")


if __name__ == "__main__":
    cli()
