"""Warming session runner — entry point for device automation.

Uses direct WDA HTTP API (no Appium middleware needed).

Usage:
    python -m sovi.device.runner --device b --platform tiktok --phase passive
    python -m sovi.device.runner --device all --platform instagram --phase light
    python -m sovi.device.runner --device b --platform tiktok --phase passive --duration 45
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from sovi.device.device_registry import get_active_devices, to_wda_device
from sovi.device.wda_client import WDADevice, WDASession
from sovi.device.warming import WarmingConfig, WarmingPhase, run_warming

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

def _load_devices() -> dict[str, WDADevice]:
    """Load devices from DB registry, falling back to empty dict."""
    try:
        rows = get_active_devices()
        return {
            (row.get("name") or row.get("label", "unknown")): to_wda_device(row)
            for row in rows
        }
    except Exception:
        logger.warning("Could not load devices from registry", exc_info=True)
        return {}

# Niche hashtags for algorithm training
NICHE_HASHTAGS = {
    "personal_finance": [
        "personalfinance", "budgeting", "savingmoney", "investing",
        "financetips", "moneytips", "debtfree", "sidehustle",
    ],
    "ai_storytelling": [
        "aiart", "aistorytelling", "darkstories", "creepystories",
        "aifilm", "generativeart",
    ],
    "tech_ai_tools": [
        "aitools", "techtools", "productivity", "chatgpt",
        "artificial_intelligence", "techreview",
    ],
}

PHASE_MAP = {
    "passive": WarmingPhase.PASSIVE,
    "light": WarmingPhase.LIGHT,
    "moderate": WarmingPhase.MODERATE,
    "active": WarmingPhase.ACTIVE,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="SOVI Warming Runner")
    parser.add_argument("--device", default="all", help="Device name or 'all'")
    parser.add_argument("--platform", choices=["tiktok", "instagram", "reddit"], required=True)
    parser.add_argument("--phase", choices=list(PHASE_MAP), default="passive")
    parser.add_argument("--niche", default="personal_finance")
    parser.add_argument("--duration", type=int, default=30, help="Duration in minutes")
    args = parser.parse_args()

    devices = _load_devices()
    if not devices:
        logger.error("No active devices found in registry")
        sys.exit(1)

    if args.device == "all":
        targets = list(devices.values())
    elif args.device in devices:
        targets = [devices[args.device]]
    else:
        logger.error("Device '%s' not found. Available: %s", args.device, list(devices.keys()))
        sys.exit(1)

    hashtags = NICHE_HASHTAGS.get(args.niche, [])

    for device in targets:
        # Check if this device has the app
        logger.info("=== %s: %s %s (phase=%s, %d min) ===", device.name, args.platform, args.niche, args.phase, args.duration)

        session = WDASession(device)
        try:
            session.connect()
            logger.info("Connected (session %s)", session.session_id[:8])

            config = WarmingConfig(
                device_name=device.name,
                platform=args.platform,
                phase=PHASE_MAP[args.phase],
                niche_hashtags=hashtags,
                duration_min=args.duration,
            )

            result = run_warming(session, config)
            result["device"] = device.name
            result["platform"] = args.platform
            result["niche"] = args.niche
            result["timestamp"] = datetime.now().isoformat()

            # Save log
            log_path = f"/tmp/sovi-warming-{device.name}-{args.platform}-{datetime.now():%Y%m%d_%H%M%S}.json"
            with open(log_path, "w") as f:
                json.dump(result, f, indent=2)
            logger.info("Result: %s", json.dumps(result, indent=2))
            logger.info("Log saved: %s", log_path)

        except Exception:
            logger.error("Failed on %s", device.name, exc_info=True)
        finally:
            session.disconnect()
            logger.info("Disconnected from %s", device.name)

    logger.info("=== Done ===")


if __name__ == "__main__":
    main()
