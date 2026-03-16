"""SOVI CLI — unified entry point for all tools.

Usage:
    sovi health                # System health check
    sovi status                # Show system status summary
    sovi db-check              # Verify database connectivity
    sovi niche-info --slug X   # Show niche configuration
    sovi warm                  # Run warming session (legacy)
    sovi produce               # Produce video from topic
    sovi dry-run               # Dry-run pipeline validation
    sovi research              # Run research scanner
    sovi db                    # Database summary
    sovi server                # Start dashboard (FastAPI on port 8888)
    sovi scheduler start       # Start continuous scheduler daemon
    sovi scheduler status      # Show device threads + current tasks
    sovi accounts list         # List accounts
    sovi devices list          # List devices
    sovi devices add           # Register new device
    sovi personas generate     # Generate personas for a niche
    sovi personas generate-all # Generate for all active niches
    sovi personas photos       # Generate photos for pending personas
    sovi personas list         # List personas
    sovi personas pipeline     # Show pipeline status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from sovi.db import sync_conn


def cmd_health(args: argparse.Namespace) -> None:
    """Run system health check."""
    from sovi.cli.health_check import main as health_main
    health_main()


def cmd_status(args: argparse.Namespace) -> None:
    """Show system status summary."""
    from sovi.config import settings
    print("\nSOVI System Status")
    print(f"  Database: {settings.database_url.split('@')[-1]}")
    print(f"  Redis: {settings.redis_url}")
    print(f"  Temporal: {settings.temporal_host}")
    print(f"  Device Daemon: {settings.device_daemon_host}")
    print(f"  Video Target: {settings.daily_video_target}/day")
    print(f"  Default Tier: {settings.default_video_tier}")
    print()


def cmd_db_check(args: argparse.Namespace) -> None:
    """Verify database connectivity."""
    from sovi.db import close_pool, execute_one, init_pool

    async def _check() -> None:
        await init_pool(min_size=1, max_size=1)
        row = await execute_one("SELECT 1 AS ok")
        if row and row["ok"] == 1:
            print("Database connection OK")
        else:
            print("Database check failed")
        await close_pool()

    asyncio.run(_check())


def cmd_niche_info(args: argparse.Namespace) -> None:
    """Show niche configuration."""
    from sovi.config import load_niche_config
    slug = args.slug
    if not slug:
        print("Error: --slug is required")
        sys.exit(1)
    try:
        cfg = load_niche_config(slug)
        print(json.dumps(cfg, indent=2, default=str))
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)


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
        conn = sync_conn()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        sys.exit(1)

    with conn.cursor() as cur:
        # Table counts
        tables = [
            "niches", "personas", "persona_photos", "email_accounts",
            "hooks", "trending_topics", "content",
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
        conn = sync_conn()
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


def cmd_personas(args: argparse.Namespace) -> None:
    """Manage personas."""
    action = args.action

    if action == "generate":
        niche = args.niche
        count = args.count
        if not niche:
            print("Error: --niche is required")
            sys.exit(1)
        from sovi.persona import create_persona_batch
        print(f"Generating {count} personas for niche: {niche}...")
        ids = create_persona_batch(niche, count)
        print(f"Created {len(ids)} personas")
        for pid in ids:
            print(f"  {pid}")

    elif action == "generate-all":
        count = args.count
        from sovi.config import load_all_niche_configs
        from sovi.persona import create_persona_batch
        niches = load_all_niche_configs()
        if not niches:
            print("No niche configs found")
            sys.exit(1)

        total = 0
        for slug in niches:
            print(f"\nGenerating {count} personas for {slug}...")
            try:
                ids = create_persona_batch(slug, count)
                total += len(ids)
                print(f"  Created {len(ids)} personas")
            except Exception as e:
                print(f"  Error: {e}")

        print(f"\nTotal created: {total} personas across {len(niches)} niches")

    elif action == "photos":
        limit = args.count
        from sovi.persona import generate_photos_for_pending
        print(f"Generating photos for up to {limit} personas...")
        count = generate_photos_for_pending(limit=limit)
        print(f"Generated photos for {count} personas")

    elif action == "list":
        conn = sync_conn()
        with conn.cursor() as cur:
            conditions = []
            params: list = []
            if hasattr(args, "niche") and args.niche:
                conditions.append("n.slug = %s")
                params.append(args.niche)
            if hasattr(args, "status") and args.status:
                conditions.append("p.status = %s")
                params.append(args.status)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            cur.execute(
                f"""SELECT p.display_name, p.age, p.gender, p.occupation,
                           p.status, p.photos_generated,
                           n.slug as niche,
                           (SELECT COUNT(*) FROM email_accounts ea WHERE ea.persona_id = p.id) as email_count,
                           (SELECT COUNT(*) FROM accounts a WHERE a.persona_id = p.id AND a.deleted_at IS NULL) as account_count
                    FROM personas p
                    JOIN niches n ON p.niche_id = n.id
                    {where}
                    ORDER BY p.created_at DESC""",
                tuple(params),
            )
            rows = cur.fetchall()

            print(f"\n{'Name':<22} {'Age':>4} {'Gender':<8} {'Niche':<20} {'Email':>5} {'Accts':>5} {'Photos':>6} {'Status':<8}")
            print("-" * 95)
            for r in rows:
                photos = "yes" if r["photos_generated"] else "no"
                print(f"  {r['display_name']:<20} {r['age']:>4} {r['gender']:<8} {r['niche']:<20} {r['email_count']:>5} {r['account_count']:>5} {photos:>6} {r['status']:<8}")

            print(f"\n  Total: {len(rows)} personas\n")
        conn.close()

    elif action == "pipeline":
        conn = sync_conn()
        with conn.cursor() as cur:
            # Summary stats
            cur.execute("SELECT COUNT(*) as cnt FROM personas")
            total = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM personas WHERE status = 'ready'")
            ready = cur.fetchone()["cnt"]

            cur.execute(
                """SELECT COUNT(DISTINCT p.id) as cnt
                   FROM personas p
                   JOIN email_accounts ea ON ea.persona_id = p.id"""
            )
            with_email = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM personas WHERE photos_generated = true")
            with_photos = cur.fetchone()["cnt"]

            cur.execute(
                """SELECT a.platform, COUNT(*) as cnt
                   FROM accounts a
                   WHERE a.persona_id IS NOT NULL AND a.deleted_at IS NULL
                   GROUP BY a.platform ORDER BY a.platform"""
            )
            platform_counts = cur.fetchall()

            total_accounts = sum(p["cnt"] for p in platform_counts)

            print(f"\n  Persona Pipeline Status")
            print(f"  {'='*40}")
            print(f"  Total personas:    {total}")
            print(f"  Ready:             {ready}")
            print(f"  With email:        {with_email}/{total}")
            print(f"  With photos:       {with_photos}/{total}")
            print(f"  Platform accounts: {total_accounts}/{total * 6}")
            if platform_counts:
                print(f"\n  By platform:")
                for p in platform_counts:
                    print(f"    {p['platform']:<18} {p['cnt']:>4}")

            # Per-niche breakdown
            cur.execute(
                """SELECT n.slug, COUNT(p.id) as personas,
                          COUNT(DISTINCT ea.id) as emails,
                          COUNT(DISTINCT a.id) as accounts
                   FROM niches n
                   LEFT JOIN personas p ON p.niche_id = n.id
                   LEFT JOIN email_accounts ea ON ea.persona_id = p.id
                   LEFT JOIN accounts a ON a.persona_id = p.id AND a.deleted_at IS NULL
                   WHERE n.status = 'active'
                   GROUP BY n.slug
                   ORDER BY n.slug"""
            )
            niche_rows = cur.fetchall()
            if niche_rows:
                print(f"\n  By niche:")
                print(f"  {'Niche':<25} {'Personas':>8} {'Emails':>7} {'Accounts':>8}")
                print(f"  {'-'*50}")
                for r in niche_rows:
                    print(f"    {r['slug']:<23} {r['personas']:>8} {r['emails']:>7} {r['accounts']:>8}")

            print()
        conn.close()


def cmd_devices(args: argparse.Namespace) -> None:
    """Manage devices."""
    action = args.action

    if action == "list":
        conn = sync_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM devices ORDER BY label")
            rows = cur.fetchall()

            print(f"\n{'Name':<15} {'Model':<12} {'iOS':<6} {'Port':>5} {'Status':<14} {'UDID'}")
            print("-" * 80)
            for r in rows:
                print(f"  {(r['label'] or '?'):<13} {r['model']:<12} {r['ios_version']:<6} {(r['appium_port'] or 0):>5} {r['status']:<14} {r['udid'][:20]}...")

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
            print(f"Registered device: {result['label']} (port {result['appium_port']})")
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

    # health / status / db-check / niche-info
    sub.add_parser("health", help="System health check")
    sub.add_parser("status", help="Show system status summary")
    sub.add_parser("db-check", help="Verify database connectivity")
    p_niche = sub.add_parser("niche-info", help="Show niche configuration")
    p_niche.add_argument("--slug", required=True, help="Niche slug")

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

    # personas
    p_personas = sub.add_parser("personas", help="Manage personas")
    p_personas.add_argument("action", choices=["generate", "generate-all", "photos", "list", "pipeline"])
    p_personas.add_argument("--niche", help="Niche slug")
    p_personas.add_argument("--count", type=int, default=10, help="Number to generate")
    p_personas.add_argument("--status", help="Filter by status")

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
        "status": cmd_status,
        "db-check": cmd_db_check,
        "niche-info": cmd_niche_info,
        "warm": cmd_warm,
        "produce": cmd_produce,
        "dry-run": cmd_dry_run,
        "research": cmd_research,
        "db": cmd_db,
        "server": cmd_server,
        "scheduler": cmd_scheduler,
        "accounts": cmd_accounts,
        "personas": cmd_personas,
        "devices": cmd_devices,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
