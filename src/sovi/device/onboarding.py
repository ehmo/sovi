"""Device fleet onboarding — register phones, generate plists, health check.

Handles adding 8 new iPhones to the fleet alongside the 2 existing ones.
Generates iproxy + WDA launchd plists and verifies all devices are reachable.

Usage:
    python -m sovi.device.onboarding --discover      # auto-detect USB devices
    python -m sovi.device.onboarding --check          # health check all
    python -m sovi.device.onboarding --plists         # generate launchd plists
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import time

import httpx

from sovi.device.device_registry import (
    generate_launchd_plists,
    get_active_devices,
    register_device,
)
from sovi.device.roles import WARMER, bootstrap_roles, get_seeders, get_warmers

logger = logging.getLogger(__name__)

# Port range for WDA (one per device)
WDA_PORT_START = 8100
WDA_PORT_END = 8109


def discover_devices() -> list[dict]:
    """Discover connected iOS devices via idevice_id (libimobiledevice)."""
    try:
        result = subprocess.run(
            ["idevice_id", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        udids = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except Exception:
        logger.warning("idevice_id not available, trying pymobiledevice3")
        try:
            result = subprocess.run(
                ["pymobiledevice3", "usbmux", "list", "--no-color"],
                capture_output=True, text=True, timeout=10,
            )
            # Parse JSON output
            import json
            devices = json.loads(result.stdout) if result.stdout.strip() else []
            udids = [d.get("UniqueDeviceID", d.get("SerialNumber", "")) for d in devices]
        except Exception:
            logger.error("Cannot discover devices — install libimobiledevice or pymobiledevice3")
            return []

    discovered = []
    for i, udid in enumerate(udids):
        port = WDA_PORT_START + i
        name = f"iPhone-{chr(65 + i)}"  # iPhone-A, iPhone-B, ...
        discovered.append({
            "name": name,
            "udid": udid,
            "model": "iPhone",
            "ios_version": "18.3",
            "wda_port": port,
        })
        logger.info("Discovered: %s (UDID: %s, port: %d)", name, udid[:16], port)

    return discovered


def register_fleet(devices: list[dict] | None = None) -> list[dict]:
    """Register all devices in the DB. Auto-discovers if devices not provided."""
    if devices is None:
        devices = discover_devices()

    if not devices:
        logger.error("No devices to register")
        return []

    registered = []
    for d in devices:
        row = register_device(
            name=d["name"],
            udid=d["udid"],
            model=d.get("model", "iPhone"),
            ios_version=d.get("ios_version", "18.3"),
            wda_port=d["wda_port"],
        )
        if row:
            registered.append(row)
            logger.info("Registered: %s (port %d)", d["name"], d["wda_port"])
        else:
            logger.error("Failed to register: %s", d["name"])

    return registered


def generate_all_plists(output_dir: str | None = None) -> list[str]:
    """Generate iproxy + WDA launchd plists for all active devices."""
    devices = get_active_devices()
    if not devices:
        logger.error("No active devices")
        return []

    all_paths = []
    for d in devices:
        paths = generate_launchd_plists(d, output_dir)
        all_paths.extend(paths)

    logger.info("Generated %d plist files for %d devices", len(all_paths), len(devices))
    return all_paths


def health_check_all() -> dict[str, bool]:
    """Check WDA health on all active devices. Returns {name: healthy}."""
    devices = get_active_devices()
    results = {}

    for d in devices:
        name = d["name"] or str(d["udid"])[:12]
        port = d["wda_port"] or 8100
        url = f"http://localhost:{port}/status"

        try:
            resp = httpx.get(url, timeout=10.0)
            ready = resp.json().get("value", {}).get("ready", False)
            results[name] = ready
            status = "OK" if ready else "NOT READY"
            logger.info("  %s (port %d): %s", name, port, status)
        except Exception as e:
            results[name] = False
            logger.warning("  %s (port %d): UNREACHABLE (%s)", name, port, e)

    healthy = sum(1 for v in results.values() if v)
    total = len(results)
    logger.info("Health check: %d/%d devices healthy", healthy, total)

    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Device fleet onboarding")
    parser.add_argument("--discover", action="store_true", help="Discover and register USB devices")
    parser.add_argument("--check", action="store_true", help="Health check all devices")
    parser.add_argument("--plists", action="store_true", help="Generate launchd plists")
    parser.add_argument("--bootstrap-roles", action="store_true", help="Bootstrap seeder/warmer roles")
    parser.add_argument("--status", action="store_true", help="Show fleet status")
    args = parser.parse_args()

    if args.discover:
        devices = discover_devices()
        if devices:
            registered = register_fleet(devices)
            print(f"\nRegistered {len(registered)} devices")

    if args.plists:
        paths = generate_all_plists()
        print(f"\nGenerated {len(paths)} plist files:")
        for p in paths:
            print(f"  {p}")
        print("\nTo activate:")
        print("  launchctl load ~/Library/LaunchAgents/com.sovi.iproxy-*.plist")
        print("  launchctl load ~/Library/LaunchAgents/com.sovi.wda-*.plist")

    if args.check:
        print("\nHealth check:")
        health_check_all()

    if args.bootstrap_roles:
        bootstrap_roles()
        print("\nRoles bootstrapped:")
        for s in get_seeders():
            print(f"  SEEDER: {s['name']}")
        for w in get_warmers():
            print(f"  WARMER: {w['name']}")

    if args.status:
        print("\nFleet status:")
        devices = get_active_devices()
        for d in devices:
            role = d.get("current_role", "idle") or "idle"
            print(f"  {d['name']}: {role} (port {d['wda_port']}, {d['status']})")


if __name__ == "__main__":
    main()
