"""System health check — quick overview of all SOVI components.

Usage:
    python -m sovi.cli.health_check
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
import psycopg.rows

# Ensure homebrew on PATH
_brew = "/opt/homebrew/bin"
if _brew not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_brew}:{os.environ.get('PATH', '')}"

# Fallback device config (used when DB is unreachable)
_FALLBACK_DEVICES = [
    {"name": "iPhone-A", "udid": "00008140-001975DC3678801C", "wda_port": 8100},
    {"name": "iPhone-B", "udid": "00008140-001A00141163001C", "wda_port": 8101},
]

APPS = {
    "com.zhiliaoapp.musically": "TikTok",
    "com.burbn.instagram": "Instagram",
    "com.google.ios.youtube": "YouTube",
    "com.reddit.Reddit": "Reddit",
    "com.atebits.Tweetie2": "X/Twitter",
}

DB_URL = "postgresql://sovi:sovi@localhost:5432/sovi"


def _get_devices() -> list[dict]:
    """Get device list from DB, falling back to hardcoded list."""
    try:
        conn = psycopg.connect(DB_URL, row_factory=psycopg.rows.dict_row)
        with conn.cursor() as cur:
            cur.execute("SELECT name, udid, wda_port, status FROM devices ORDER BY name")
            rows = cur.fetchall()
        conn.close()
        if rows:
            return rows
    except Exception:
        pass
    return _FALLBACK_DEVICES


DEVICES = _get_devices()

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg: str) -> str:
    return f"{GREEN}OK{RESET} {msg}"


def fail(msg: str) -> str:
    return f"{RED}FAIL{RESET} {msg}"


def warn(msg: str) -> str:
    return f"{YELLOW}WARN{RESET} {msg}"


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}--- {title} ---{RESET}")


def check_wda() -> None:
    """Check WDA status on all devices."""
    header("WDA / Device Status")
    for dev in DEVICES:
        port = dev["wda_port"]
        try:
            resp = httpx.get(f"http://localhost:{port}/status", timeout=5.0)
            data = resp.json()
            value = data.get("value", {})
            ready = value.get("ready", False)
            ios_ver = value.get("os", {}).get("version", "?")
            ip = value.get("ios", {}).get("ip", "?")
            wda_ver = value.get("build", {}).get("version", "?")
            if ready:
                print(f"  {ok(dev['name'])}: iOS {ios_ver}, WDA {wda_ver}, IP {ip}, port {port}")
            else:
                print(f"  {warn(dev['name'])}: WDA not ready (port {port})")
        except Exception as e:
            print(f"  {fail(dev['name'])}: {e}")


def check_apps() -> None:
    """Check installed app states on all devices."""
    header("App Status")
    state_names = {1: "not running", 2: "background", 3: "suspended", 4: "foreground"}
    for dev in DEVICES:
        port = dev["wda_port"]
        # Create session
        try:
            resp = httpx.post(
                f"http://localhost:{port}/session",
                json={"capabilities": {"alwaysMatch": {}}},
                timeout=10.0,
            )
            data = resp.json()
            sid = data.get("sessionId") or data.get("value", {}).get("sessionId")
            if not sid:
                print(f"  {fail(dev['name'])}: Could not create WDA session")
                continue
        except Exception as e:
            print(f"  {fail(dev['name'])}: {e}")
            continue

        installed = []
        for bid, name in APPS.items():
            try:
                resp = httpx.post(
                    f"http://localhost:{port}/session/{sid}/wda/apps/state",
                    json={"bundleId": bid},
                    timeout=5.0,
                )
                state = resp.json().get("value", 0)
                state_str = state_names.get(state, f"unknown({state})")
                if state > 0:
                    installed.append(name)
            except Exception:
                pass

        missing = [n for n in APPS.values() if n not in installed]
        if not missing:
            print(f"  {ok(dev['name'])}: All {len(APPS)} apps installed")
        else:
            print(f"  {warn(dev['name'])}: Missing: {', '.join(missing)}")

        # Clean up session
        try:
            httpx.delete(f"http://localhost:{port}/session/{sid}", timeout=5.0)
        except Exception:
            pass


def check_database() -> dict:
    """Check database connection and table counts."""
    header("Database")
    try:
        conn = psycopg.connect(DB_URL, row_factory=psycopg.rows.dict_row)
        with conn.cursor() as cur:
            tables = {
                "niches": "Niches",
                "hooks": "Hook templates",
                "trending_topics": "Trending topics",
                "content": "Content",
                "distributions": "Distributions",
                "accounts": "Accounts",
                "devices": "Devices",
                "metric_snapshots": "Metric snapshots",
            }
            counts = {}
            for table, label in tables.items():
                try:
                    cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
                    row = cur.fetchone()
                    cnt = row["cnt"] if row else 0
                    counts[table] = cnt
                except Exception:
                    counts[table] = -1

            print(f"  {ok('Connected')}: PostgreSQL + TimescaleDB")
            for table, label in tables.items():
                cnt = counts[table]
                if cnt < 0:
                    print(f"    {fail(label)}: table error")
                elif cnt == 0:
                    print(f"    {DIM}{label}: {cnt}{RESET}")
                else:
                    print(f"    {label}: {cnt}")

        conn.close()
        return counts
    except Exception as e:
        print(f"  {fail('Connection')}: {e}")
        return {}


def check_services() -> None:
    """Check launchd service status."""
    header("Launchd Services")
    services = [
        "com.sovi.iproxy-iphone-a",
        "com.sovi.iproxy-iphone-b",
        "com.sovi.wda-iphone-a",
        "com.sovi.wda-iphone-b",
        "com.sovi.warming",
        "com.sovi.research",
    ]
    for svc in services:
        try:
            result = subprocess.run(
                ["launchctl", "list", svc],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Parse PID and exit status from launchctl plist output
                output = result.stdout
                short = svc.replace("com.sovi.", "")
                pid = None
                exit_status = None
                for line in output.strip().split("\n"):
                    if '"PID"' in line:
                        pid = line.split("=")[1].strip().rstrip(";")
                    elif '"LastExitStatus"' in line:
                        exit_status = line.split("=")[1].strip().rstrip(";")

                if pid:
                    print(f"  {ok(short)}: running (PID {pid})")
                elif exit_status == "0":
                    print(f"  {ok(short)}: idle (last exit 0)")
                elif exit_status:
                    print(f"  {warn(short)}: not running (last exit {exit_status})")
                else:
                    print(f"  {ok(short)}: loaded")
            else:
                short = svc.replace("com.sovi.", "")
                print(f"  {fail(short)}: not loaded")
        except Exception as e:
            short = svc.replace("com.sovi.", "")
            print(f"  {fail(short)}: {e}")


def check_api_keys() -> None:
    """Check which API keys are configured."""
    header("API Keys (.env)")
    env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if not env_path.exists():
        # Try alternate location
        env_path = Path.cwd() / ".env"

    keys = {
        "ANTHROPIC_API_KEY": "Claude (scripts)",
        "FAL_KEY": "fal.ai (images/video)",
        "OPENAI_API_KEY": "OpenAI TTS",
        "ELEVENLABS_API_KEY": "ElevenLabs (voice)",
        "DEEPGRAM_API_KEY": "Deepgram (transcription)",
        "LATE_API_KEY": "Late (distribution)",
        "REDDIT_CLIENT_ID": "Reddit API",
    }

    configured = set()
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key = line.split("=", 1)[0].strip()
                val = line.split("=", 1)[1].strip()
                if key in keys and val and val not in ('""', "''", ""):
                    configured.add(key)

    # Also check environment
    for key in keys:
        if os.environ.get(key):
            configured.add(key)

    for key, label in keys.items():
        if key in configured:
            print(f"  {ok(label)}")
        else:
            print(f"  {fail(label)}: not configured")


def check_disk() -> None:
    """Check disk usage for output directory."""
    header("Disk Usage")
    output_dir = Path.cwd() / "output"
    if output_dir.exists():
        result = subprocess.run(
            ["du", "-sh", str(output_dir)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            size = result.stdout.strip().split("\t")[0]
            print(f"  Output dir: {size}")
        else:
            print(f"  Output dir: error reading")
    else:
        print(f"  {DIM}Output dir: not created yet{RESET}")

    # Overall disk
    result = subprocess.run(
        ["df", "-h", "/"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        lines = result.stdout.strip().split("\n")
        if len(lines) > 1:
            parts = lines[1].split()
            if len(parts) >= 5:
                print(f"  System disk: {parts[3]} available ({parts[4]} used)")


def check_ffmpeg() -> None:
    """Check FFmpeg availability."""
    header("FFmpeg")
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ver_line = result.stdout.split("\n")[0]
            print(f"  {ok(ver_line)}")

            # Check for key features
            config = result.stdout
            features = {
                "--enable-libass": "libass (captions)",
                "--enable-libfreetype": "libfreetype (text)",
                "--enable-libfdk-aac": "fdk-aac (audio)",
            }
            for flag, name in features.items():
                if flag in config:
                    print(f"    {ok(name)}")
                else:
                    print(f"    {warn(name)}: not compiled in")
        else:
            print(f"  {fail('ffmpeg not working')}")
    except FileNotFoundError:
        print(f"  {fail('ffmpeg not found on PATH')}")


def check_scheduler() -> None:
    """Check scheduler status and account fleet stats."""
    header("Scheduler & Fleet")

    # Check if dashboard is running
    try:
        resp = httpx.get("http://localhost:8888/api/overview", timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  {ok('Dashboard')}: http://localhost:8888")
            print(f"    Accounts: {data.get('total_accounts', 0)}")
            print(f"    Active devices: {data.get('active_devices', 0)}")
            print(f"    Sessions today: {data.get('sessions_today', 0)}")
            print(f"    Unresolved errors: {data.get('error_count', 0)}")
        else:
            print(f"  {warn('Dashboard')}: returned {resp.status_code}")
    except Exception:
        print(f"  {DIM}Dashboard: not running (start with: sovi server){RESET}")

    # Check scheduler status via API
    try:
        resp = httpx.get("http://localhost:8888/api/scheduler/status", timeout=3.0)
        if resp.status_code == 200:
            status = resp.json()
            if status.get("running"):
                print(f"  {ok('Scheduler')}: {status.get('device_count', 0)} device threads")
            else:
                print(f"  {DIM}Scheduler: not running{RESET}")
    except Exception:
        pass

    # Account fleet summary from DB
    try:
        conn = psycopg.connect(DB_URL, row_factory=psycopg.rows.dict_row)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT platform, current_state, COUNT(*) as cnt
                   FROM accounts
                   WHERE deleted_at IS NULL
                   GROUP BY platform, current_state
                   ORDER BY platform, current_state"""
            )
            rows = cur.fetchall()
            if rows:
                print(f"\n  {'Platform':<12} {'State':<15} {'Count':>6}")
                print(f"  {'-'*35}")
                for r in rows:
                    print(f"  {r['platform']:<12} {r['current_state']:<15} {r['cnt']:>6}")
        conn.close()
    except Exception:
        pass


def main() -> None:
    print(f"\n{BOLD}SOVI System Health Check{RESET}")
    print(f"{DIM}{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}{RESET}")

    check_wda()
    check_apps()
    check_database()
    check_scheduler()
    check_services()
    check_api_keys()
    check_ffmpeg()
    check_disk()

    print(f"\n{BOLD}Done.{RESET}\n")


if __name__ == "__main__":
    main()
