"""SOVI CLI — unified entry point for all tools.

Usage:
    python -m sovi health          # System health check
    python -m sovi warm             # Run warming session (legacy)
    python -m sovi produce          # Produce video from topic
    python -m sovi dry-run          # Dry-run pipeline validation
    python -m sovi research         # Run research scanner
    python -m sovi db               # Database summary
    python -m sovi server           # Start dashboard (FastAPI on port 8888)
    python -m sovi scheduler start  # Start continuous scheduler daemon
    python -m sovi scheduler status # Show device threads + current tasks
    python -m sovi accounts list    # List accounts
    python -m sovi devices list     # List devices
    python -m sovi devices add      # Register new device
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

import psycopg
import psycopg.rows


DB_URL = "postgresql://sovi:sovi@localhost:5432/sovi"


def cmd_health(args: argparse.Namespace) -> None:
    """Run system health check."""
    from sovi.cli.health_check import main as health_main
    health_main()


def cmd_warm(args: argparse.Namespace) -> None:
    """Run warming session."""
    from sovi.device.warm_all import main as warm_main
    # Patch sys.argv so argparse inside warm_all picks up our args
    argv = ["warm"]
    if args.duration:
        argv.extend(["--duration", str(args.duration)])
    if args.phase:
        argv.extend(["--phase", args.phase])
    sys.argv = argv
    warm_main()


def cmd_produce(args: argparse.Namespace) -> None:
    """Produce a video."""
    from sovi.production.produce_video import _main
    argv = ["produce"]
    if args.topic:
        argv.extend(["--topic", args.topic])
    if args.from_db:
        argv.append("--from-db")
    if args.niche:
        argv.extend(["--niche", args.niche])
    if args.platform:
        argv.extend(["--platform", args.platform])
    if args.format:
        argv.extend(["--format", args.format])
    if args.duration:
        argv.extend(["--duration", str(args.duration)])
    if args.elevenlabs:
        argv.append("--elevenlabs")
    sys.argv = argv
    asyncio.run(_main())


def cmd_dry_run(args: argparse.Namespace) -> None:
    """Run dry-run pipeline validation."""
    from sovi.production.dry_run import _main
    argv = ["dry-run"]
    if args.topic:
        argv.extend(["--topic", args.topic])
    if args.niche:
        argv.extend(["--niche", args.niche])
    if args.platform:
        argv.extend(["--platform", args.platform])
    if args.duration:
        argv.extend(["--duration", str(args.duration)])
    sys.argv = argv
    asyncio.run(_main())


def cmd_research(args: argparse.Namespace) -> None:
    """Run research scanner."""
    from sovi.research.run_scan import main as research_main
    argv = ["research"]
    if args.reddit_only:
        argv.append("--reddit-only")
    if args.tiktok_only:
        argv.append("--tiktok-only")
    if args.stories:
        argv.append("--stories")
    sys.argv = argv
    research_main()


def cmd_db(args: argparse.Namespace) -> None:
    """Show database summary."""
    try:
        conn = psycopg.connect(DB_URL, row_factory=psycopg.rows.dict_row)
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        sys.exit(1)

    with conn.cursor() as cur:
        # Table counts
        tables = [
            "niches", "hooks", "trending_topics", "content",
            "distributions", "accounts", "devices", "metric_snapshots",
            "system_events",
        ]
        print(f"\n{'Table':<25} {'Count':>8}")
        print("-" * 35)
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) as cnt FROM {t}")
                row = cur.fetchone()
                cnt = row["cnt"] if row else 0
                print(f"  {t:<23} {cnt:>8}")
            except Exception:
                conn.rollback()
                print(f"  {t:<23} {'error':>8}")

        # Recent content
        print("\nRecent content:")
        try:
            cur.execute(
                """SELECT id, topic, production_status, quality_score,
                          created_at
                   FROM content ORDER BY created_at DESC LIMIT 5"""
            )
            rows = cur.fetchall()
            if rows:
                for r in rows:
                    ts = r["created_at"].strftime("%m/%d %H:%M") if r.get("created_at") else "?"
                    print(f"  [{r['production_status']:<8}] {ts} q={r.get('quality_score', '?'):<5} {str(r['topic'])[:60]}")
            else:
                print("  (none)")
        except Exception:
            conn.rollback()
            print("  (error reading content)")

        # Recent activity log
        print("\nRecent warming sessions:")
        try:
            cur.execute(
                """SELECT timestamp, device_id, detail_json
                   FROM activity_log
                   WHERE detail_json::text LIKE '%warming_session%'
                   ORDER BY timestamp DESC LIMIT 5"""
            )
            rows = cur.fetchall()
            if rows:
                for r in rows:
                    ts = r["timestamp"].strftime("%m/%d %H:%M") if r.get("timestamp") else "?"
                    detail = r.get("detail_json", {})
                    if isinstance(detail, str):
                        detail = json.loads(detail)
                    plat = detail.get("platform", "?")
                    phase = detail.get("phase", "?")
                    dur = detail.get("duration_min", 0)
                    print(f"  {ts} | {plat:<10} | {phase:<15} | {dur:.0f} min")
            else:
                print("  (none)")
        except Exception:
            conn.rollback()
            print("  (error reading activity log)")

        # Trending topics summary
        print("\nTrending topics by niche:")
        try:
            cur.execute(
                """SELECT n.name, COUNT(t.id) as cnt, MAX(t.trend_score) as max_score
                   FROM trending_topics t
                   JOIN niches n ON t.niche_id = n.id
                   WHERE t.is_active = true
                   GROUP BY n.name
                   ORDER BY cnt DESC"""
            )
            rows = cur.fetchall()
            for r in rows:
                print(f"  {r['name']:<25} {r['cnt']:>4} topics (max score: {r.get('max_score', 0):.0f})")
        except Exception:
            conn.rollback()
            print("  (error reading trending topics)")

    conn.close()
    print()


def cmd_server(args: argparse.Namespace) -> None:
    """Start the dashboard server."""
    import uvicorn
    from sovi.dashboard.app import app

    port = args.port if hasattr(args, "port") else 8888
    host = args.host if hasattr(args, "host") else "0.0.0.0"
    print(f"Starting SOVI Dashboard on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_scheduler(args: argparse.Namespace) -> None:
    """Manage the continuous scheduler."""
    from sovi.device.scheduler import get_scheduler

    action = args.action

    if action == "start":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        sched = get_scheduler()
        sched.start()
        print("Scheduler started. Press Ctrl+C to stop.")
        try:
            import time
            while sched.is_running:
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopping scheduler...")
            sched.stop()
            print("Scheduler stopped.")

    elif action == "status":
        sched = get_scheduler()
        status = sched.status()
        print(f"\nScheduler running: {status['running']}")
        print(f"Device threads: {status['device_count']}")
        print(f"Target: {status['sessions_per_day_target']} sessions/device/day")
        if status["threads"]:
            print(f"\n{'Device':<15} {'Task':<35} {'Sessions':>8} {'Alive':>6}")
            print("-" * 70)
            for tid, t in status["threads"].items():
                print(f"  {t['device_name']:<13} {t['current_task']:<35} {t['sessions_today']:>8} {'yes' if t['alive'] else 'no':>6}")
        else:
            print("  No threads active")
        print()

    elif action == "stop":
        sched = get_scheduler()
        sched.stop()
        print("Scheduler stopped.")


def cmd_accounts(args: argparse.Namespace) -> None:
    """Manage accounts."""
    action = args.action

    if action == "list":
        conn = psycopg.connect(DB_URL, row_factory=psycopg.rows.dict_row)
        with conn.cursor() as cur:
            conditions = ["deleted_at IS NULL"]
            params: list = []
            if hasattr(args, "platform") and args.platform:
                conditions.append("platform = %s")
                params.append(args.platform)
            if hasattr(args, "niche") and args.niche:
                conditions.append("""niche_id IN (SELECT id FROM niches WHERE slug = %s)""")
                params.append(args.niche)
            if hasattr(args, "status") and args.status:
                conditions.append("current_state = %s")
                params.append(args.status)

            where = " AND ".join(conditions)
            cur.execute(
                f"""SELECT a.platform, a.username, a.current_state, a.warming_day_count,
                           a.followers, a.last_warmed_at, n.slug as niche
                    FROM accounts a
                    LEFT JOIN niches n ON a.niche_id = n.id
                    WHERE {where}
                    ORDER BY a.created_at DESC""",
                tuple(params),
            )
            rows = cur.fetchall()

            print(f"\n{'Platform':<12} {'Username':<20} {'State':<15} {'Day':>4} {'Followers':>10} {'Niche':<20} {'Last Warmed'}")
            print("-" * 105)
            for r in rows:
                lw = r["last_warmed_at"].strftime("%m/%d %H:%M") if r.get("last_warmed_at") else "—"
                print(f"  {r['platform']:<10} {r['username']:<20} {r['current_state']:<15} {r['warming_day_count']:>4} {r['followers']:>10} {r.get('niche', '—'):<20} {lw}")

            print(f"\n  Total: {len(rows)} accounts\n")
        conn.close()

    elif action == "create":
        if not args.platform or not args.email:
            print("Error: --platform and --email are required")
            sys.exit(1)
        print(f"Account creation via CLI not yet implemented. Use the dashboard or scheduler.")
        print(f"  Platform: {args.platform}")
        print(f"  Email: {args.email}")
        print(f"  Niche: {args.niche}")


def cmd_devices(args: argparse.Namespace) -> None:
    """Manage devices."""
    action = args.action

    if action == "list":
        conn = psycopg.connect(DB_URL, row_factory=psycopg.rows.dict_row)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM devices ORDER BY name")
            rows = cur.fetchall()

            print(f"\n{'Name':<15} {'Model':<12} {'iOS':<6} {'Port':>5} {'Status':<14} {'UDID'}")
            print("-" * 80)
            for r in rows:
                print(f"  {(r['name'] or '?'):<13} {r['model']:<12} {r['ios_version']:<6} {(r['wda_port'] or 0):>5} {r['status']:<14} {r['udid'][:20]}...")

            print(f"\n  Total: {len(rows)} devices\n")
        conn.close()

    elif action == "add":
        if not args.name or not args.udid:
            print("Error: --name and --udid are required")
            sys.exit(1)
        from sovi.device.device_registry import register_device
        result = register_device(
            name=args.name,
            udid=args.udid,
            model=getattr(args, "model", "iPhone"),
            wda_port=args.wda_port,
        )
        if result:
            print(f"Registered device: {result['name']} (port {result['wda_port']})")
        else:
            print("Failed to register device")

    elif action == "setup":
        if not args.name:
            print("Error: --name is required")
            sys.exit(1)
        from sovi.device.device_registry import generate_launchd_plists, get_device_by_name
        device = get_device_by_name(args.name)
        if not device:
            print(f"Device not found: {args.name}")
            sys.exit(1)
        plists = generate_launchd_plists(device)
        print(f"Generated plists for {args.name}:")
        for p in plists:
            print(f"  {p}")
        print(f"\nLoad with: launchctl load <plist>")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sovi",
        description="SOVI — Social Video Intelligence & Distribution Network",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # health
    sub.add_parser("health", help="System health check")

    # warm
    p_warm = sub.add_parser("warm", help="Run warming session")
    p_warm.add_argument("--duration", type=int, default=30)
    p_warm.add_argument("--phase", choices=["passive", "light"], default="passive")

    # produce
    p_prod = sub.add_parser("produce", help="Produce video")
    p_prod.add_argument("--topic", help="Topic text")
    p_prod.add_argument("--from-db", action="store_true")
    p_prod.add_argument("--niche", default="personal_finance")
    p_prod.add_argument("--platform", default="tiktok")
    p_prod.add_argument("--format", default="faceless")
    p_prod.add_argument("--duration", type=int, default=45)
    p_prod.add_argument("--elevenlabs", action="store_true")

    # dry-run
    p_dry = sub.add_parser("dry-run", help="Dry-run pipeline validation")
    p_dry.add_argument("--topic", help="Topic text")
    p_dry.add_argument("--niche", default="personal_finance")
    p_dry.add_argument("--platform", default="tiktok")
    p_dry.add_argument("--duration", type=int, default=30)

    # research
    p_research = sub.add_parser("research", help="Run research scanner")
    p_research.add_argument("--reddit-only", action="store_true")
    p_research.add_argument("--tiktok-only", action="store_true")
    p_research.add_argument("--stories", action="store_true")

    # db
    sub.add_parser("db", help="Database summary")

    # server
    p_server = sub.add_parser("server", help="Start dashboard (FastAPI)")
    p_server.add_argument("--port", type=int, default=8888)
    p_server.add_argument("--host", default="0.0.0.0")

    # scheduler
    p_sched = sub.add_parser("scheduler", help="Manage continuous scheduler")
    p_sched.add_argument("action", choices=["start", "status", "stop"])

    # accounts
    p_accts = sub.add_parser("accounts", help="Manage accounts")
    p_accts.add_argument("action", choices=["list", "create"])
    p_accts.add_argument("--platform", help="Filter by platform")
    p_accts.add_argument("--niche", help="Filter by niche slug")
    p_accts.add_argument("--status", help="Filter by state")
    p_accts.add_argument("--email", help="Email for account creation")

    # devices
    p_devs = sub.add_parser("devices", help="Manage devices")
    p_devs.add_argument("action", choices=["list", "add", "setup"])
    p_devs.add_argument("--name", help="Device name")
    p_devs.add_argument("--udid", help="Device UDID")
    p_devs.add_argument("--model", default="iPhone")
    p_devs.add_argument("--wda-port", type=int, default=8100)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "health": cmd_health,
        "warm": cmd_warm,
        "produce": cmd_produce,
        "dry-run": cmd_dry_run,
        "research": cmd_research,
        "db": cmd_db,
        "server": cmd_server,
        "scheduler": cmd_scheduler,
        "accounts": cmd_accounts,
        "devices": cmd_devices,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
