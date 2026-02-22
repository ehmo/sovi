"""Multi-device warming orchestrator.

Runs warming sessions across all available devices in parallel using threads.
Each device warms one platform at a time. Logs results to PostgreSQL.

Usage:
    python -m sovi.device.warm_all --duration 30 --phase passive
    python -m sovi.device.warm_all --duration 20 --phase light
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from datetime import datetime

import httpx
import psycopg

from sovi.device.wda_client import WDADevice, WDASession
from sovi.device.warming import WarmingConfig, WarmingPhase, run_warming

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Database URL — imported from settings or use default
try:
    from sovi.config import settings
    DATABASE_URL = settings.database_url
except Exception:
    DATABASE_URL = "postgresql://sovi:sovi@localhost:5432/sovi"

# Device registry
DEVICES = {
    "a": WDADevice(name="iPhone-A", udid="00008140-001975DC3678801C", wda_port=8100),
    "b": WDADevice(name="iPhone-B", udid="00008140-001A00141163001C", wda_port=8101),
}

# What each device warms — all 5 social apps on both phones.
# Split platforms across devices to keep session times reasonable.
# Each platform gets ~30 min passive or ~20 min engagement.
# iPhone A: Reddit, TikTok, YouTube
# iPhone B: Instagram, X/Twitter, TikTok
DEVICE_ASSIGNMENTS = {
    "a": ["reddit", "tiktok", "youtube"],
    "b": ["instagram", "twitter", "tiktok"],
}

PHASE_MAP = {
    "passive": WarmingPhase.PASSIVE,
    "light": WarmingPhase.LIGHT,
}


def _log_warming_to_db(result: dict) -> None:
    """Log a warming session result to the database (best-effort)."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Look up device ID
                cur.execute(
                    "SELECT id FROM devices WHERE name = %s",
                    (result.get("device"),),
                )
                device_row = cur.fetchone()
                device_id = device_row[0] if device_row else None

                # Map platform name to activity details
                platform = result.get("platform", "unknown")
                phase = result.get("phase", "passive")
                videos = result.get("videos_watched", 0)
                posts_viewed = result.get("posts_viewed", 0)
                duration = result.get("duration_min", 0)
                niche = result.get("niche", "personal_finance")

                # Insert activity_log entry
                cur.execute(
                    """INSERT INTO activity_log
                       (device_id, account_id, activity_type, detail_json, success, timestamp)
                       VALUES (
                           %(device_id)s,
                           '00000000-0000-0000-0000-000000000000'::uuid,
                           'scroll',
                           %(detail)s,
                           %(success)s,
                           now()
                       )""",
                    {
                        "device_id": device_id,
                        "detail": json.dumps({
                            "type": "warming_session",
                            "platform": platform,
                            "phase": phase,
                            "niche": niche,
                            "duration_min": duration,
                            "videos_watched": videos,
                            "posts_viewed": posts_viewed,
                        }),
                        "success": not result.get("error", False),
                    },
                )
                conn.commit()
                logger.info("Logged warming to DB: %s %s", result.get("device"), platform)
    except Exception:
        logger.warning("Failed to log warming to DB (non-fatal)", exc_info=True)


def _wait_for_wda(device: WDADevice, timeout: float = 60.0) -> bool:
    """Wait for WDA to become responsive with a short timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{device.base_url}/status", timeout=5.0)
            if resp.status_code == 200 and resp.json().get("value", {}).get("ready"):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def warm_device(device: WDADevice, platforms: list[str], phase: WarmingPhase, duration_min: int) -> list[dict]:
    """Run warming for multiple platforms on one device sequentially."""
    results = []
    for i, platform in enumerate(platforms):
        logger.info("=== %s: %s (phase=%s, %d min) ===", device.name, platform, phase.name, duration_min)

        # Between platforms, wait for WDA to settle after previous disconnect
        if i > 0:
            logger.info("Waiting for %s to settle before next platform", device.name)
            time.sleep(10)

        # Wait for WDA to be responsive before creating session
        if not _wait_for_wda(device):
            logger.error("WDA not responding on %s, skipping %s", device.name, platform)
            skip_result = {"device": device.name, "platform": platform, "error": True}
            results.append(skip_result)
            _log_warming_to_db(skip_result)
            continue

        session = WDASession(device)
        try:
            session.connect()
            config = WarmingConfig(
                device_name=device.name,
                platform=platform,
                phase=phase,
                duration_min=duration_min,
            )
            result = run_warming(session, config)
            result["device"] = device.name
            result["platform"] = platform
            result["timestamp"] = datetime.now().isoformat()
            results.append(result)

            log_path = f"/tmp/sovi-warming-{device.name}-{platform}-{datetime.now():%Y%m%d_%H%M%S}.json"
            with open(log_path, "w") as f:
                json.dump(result, f, indent=2)
            logger.info("Log saved: %s", log_path)
            _log_warming_to_db(result)
        except Exception:
            logger.error("Failed %s on %s", platform, device.name, exc_info=True)
            err_result = {"device": device.name, "platform": platform, "error": True}
            results.append(err_result)
            _log_warming_to_db(err_result)
        finally:
            # Press Home before disconnect to free WDA from heavy app UI
            try:
                session.press_button("home")
            except Exception:
                pass
            session.disconnect()
            time.sleep(2)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="SOVI Multi-Device Warming")
    parser.add_argument("--duration", type=int, default=30, help="Duration per platform in minutes")
    parser.add_argument("--phase", choices=list(PHASE_MAP), default="passive")
    args = parser.parse_args()

    phase = PHASE_MAP[args.phase]
    threads: list[threading.Thread] = []
    all_results: list[list[dict]] = []

    for device_key, platforms in DEVICE_ASSIGNMENTS.items():
        device = DEVICES[device_key]
        result_holder: list[dict] = []
        all_results.append(result_holder)

        t = threading.Thread(
            target=lambda d, p, r: r.extend(warm_device(d, p, phase, args.duration)),
            args=(device, platforms, result_holder),
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Summary
    logger.info("=== All warming complete ===")
    for results in all_results:
        for r in results:
            if r.get("error"):
                logger.warning("FAILED: %s %s", r["device"], r["platform"])
            else:
                logger.info("OK: %s %s — %s", r["device"], r["platform"],
                           {k: v for k, v in r.items() if k not in ("device", "platform", "timestamp")})


if __name__ == "__main__":
    main()
