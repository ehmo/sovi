"""CLI entry point for SOVI."""

from __future__ import annotations

import asyncio

import click
from rich.console import Console

console = Console()


@click.group()
def main() -> None:
    """SOVI — Social Video Intelligence & Distribution Network."""


@main.command()
def status() -> None:
    """Show system status."""
    from sovi.config import settings

    console.print("[bold]SOVI System Status[/bold]")
    console.print(f"  Database: {settings.database_url.split('@')[-1]}")
    console.print(f"  Redis: {settings.redis_url}")
    console.print(f"  Temporal: {settings.temporal_host}")
    console.print(f"  Device Daemon: {settings.device_daemon_host}")
    console.print(f"  Video Target: {settings.daily_video_target}/day")
    console.print(f"  Default Tier: {settings.default_video_tier}")


@main.command()
def db_check() -> None:
    """Verify database connectivity."""
    from sovi.db import close_pool, execute_one, init_pool

    async def _check() -> None:
        await init_pool(min_size=1, max_size=1)
        row = await execute_one("SELECT 1 AS ok")
        if row and row["ok"] == 1:
            console.print("[green]Database connection OK[/green]")
        else:
            console.print("[red]Database check failed[/red]")
        await close_pool()

    asyncio.run(_check())


@main.command()
@click.argument("niche_slug")
def niche_info(niche_slug: str) -> None:
    """Show niche configuration."""
    from sovi.config import load_niche_config

    try:
        cfg = load_niche_config(niche_slug)
        console.print_json(data=cfg)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")


if __name__ == "__main__":
    main()
