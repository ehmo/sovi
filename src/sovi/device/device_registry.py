"""DB-driven device registry — replaces hardcoded DEVICES dict.

All device queries go through here. The scheduler and dashboard
use these functions to discover and manage the device fleet.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from sovi.db import execute, execute_one, sync_execute, sync_execute_one
from sovi.device.wda_client import WDADevice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync helpers (for scheduler threads)
# ---------------------------------------------------------------------------


def get_active_devices() -> list[dict[str, Any]]:
    """Get all devices with status='active', ordered by name."""
    return sync_execute(
        """SELECT id, name, model, udid, ios_version, wda_port, status,
                  connected_since, battery_level, storage_free_gb
           FROM devices
           WHERE status = 'active'
           ORDER BY name"""
    )


def get_device_by_id(device_id: UUID | str) -> dict[str, Any] | None:
    return sync_execute_one(
        "SELECT * FROM devices WHERE id = %s",
        (str(device_id),),
    )


def get_device_by_name(name: str) -> dict[str, Any] | None:
    return sync_execute_one(
        "SELECT * FROM devices WHERE name = %s",
        (name,),
    )


def to_wda_device(row: dict[str, Any]) -> WDADevice:
    """Convert a DB device row to a WDADevice for WDA operations."""
    return WDADevice(
        name=row["name"] or row["udid"][:12],
        udid=row["udid"],
        wda_port=row["wda_port"] or 8100,
    )


def update_heartbeat(device_id: UUID | str) -> None:
    """Update device heartbeat timestamp."""
    sync_execute(
        "UPDATE devices SET updated_at = now(), status = 'active' WHERE id = %s",
        (str(device_id),),
    )


def set_device_status(device_id: UUID | str, status: str) -> None:
    """Set device status (active, maintenance, failed, disconnected)."""
    sync_execute(
        "UPDATE devices SET status = %s, updated_at = now() WHERE id = %s",
        (status, str(device_id)),
    )


def register_device(
    name: str,
    udid: str,
    model: str = "iPhone",
    ios_version: str = "18.3",
    wda_port: int = 8100,
) -> dict[str, Any] | None:
    """Register a new device or update an existing one (by UDID)."""
    rows = sync_execute(
        """INSERT INTO devices (name, model, udid, ios_version, wda_port, status, connected_since)
           VALUES (%s, %s, %s, %s, %s, 'active', now())
           ON CONFLICT (udid) DO UPDATE SET
               name = EXCLUDED.name,
               wda_port = EXCLUDED.wda_port,
               ios_version = EXCLUDED.ios_version,
               status = 'active',
               connected_since = now(),
               updated_at = now()
           RETURNING *""",
        (name, model, udid, ios_version, wda_port),
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Async helpers (for dashboard)
# ---------------------------------------------------------------------------


async def async_get_devices() -> list[dict[str, Any]]:
    """Get all devices (async, for dashboard)."""
    return await execute(
        """SELECT id, name, model, udid, ios_version, wda_port, status,
                  connected_since, battery_level, storage_free_gb,
                  created_at, updated_at
           FROM devices
           ORDER BY name"""
    )


async def async_get_device(device_id: str) -> dict[str, Any] | None:
    return await execute_one(
        "SELECT * FROM devices WHERE id = %s",
        (device_id,),
    )


async def async_register_device(
    name: str, udid: str, model: str, ios_version: str, wda_port: int,
) -> dict[str, Any] | None:
    rows = await execute(
        """INSERT INTO devices (name, model, udid, ios_version, wda_port, status, connected_since)
           VALUES (%s, %s, %s, %s, %s, 'active', now())
           ON CONFLICT (udid) DO UPDATE SET
               name = EXCLUDED.name,
               wda_port = EXCLUDED.wda_port,
               ios_version = EXCLUDED.ios_version,
               status = 'active',
               connected_since = now(),
               updated_at = now()
           RETURNING *""",
        (name, model, udid, ios_version, wda_port),
    )
    return rows[0] if rows else None


async def async_get_device_sessions(device_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Get recent system_events for a device."""
    return await execute(
        """SELECT id, timestamp, category, severity, event_type, account_id, message, context
           FROM system_events
           WHERE device_id = %s
           ORDER BY timestamp DESC
           LIMIT %s""",
        (device_id, limit),
    )


# ---------------------------------------------------------------------------
# launchd plist generation
# ---------------------------------------------------------------------------


def generate_launchd_plists(device: dict[str, Any], output_dir: str | None = None) -> list[str]:
    """Generate iproxy + WDA launchd plist files for a device.

    Returns list of generated plist file paths.
    """
    if output_dir is None:
        output_dir = os.path.expanduser("~/Library/LaunchAgents")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    name = device["name"].lower().replace(" ", "-")
    udid = device["udid"]
    wda_port = device.get("wda_port", 8100)
    generated = []

    # iproxy plist
    iproxy_label = f"com.sovi.iproxy-{name}"
    iproxy_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{iproxy_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/iproxy</string>
        <string>{wda_port}</string>
        <string>8100</string>
        <string>--udid</string>
        <string>{udid}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/{iproxy_label}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{iproxy_label}.err</string>
</dict>
</plist>"""

    iproxy_path = output_path / f"{iproxy_label}.plist"
    iproxy_path.write_text(iproxy_plist)
    generated.append(str(iproxy_path))

    # WDA plist
    wda_label = f"com.sovi.wda-{name}"
    wda_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{wda_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/xcodebuild</string>
        <string>-project</string>
        <string>/opt/homebrew/lib/node_modules/appium/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj</string>
        <string>-scheme</string>
        <string>WebDriverAgentRunner</string>
        <string>-destination</string>
        <string>id={udid}</string>
        <string>test</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/{wda_label}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{wda_label}.err</string>
</dict>
</plist>"""

    wda_path = output_path / f"{wda_label}.plist"
    wda_path.write_text(wda_plist)
    generated.append(str(wda_path))

    logger.info("Generated plists for %s: %s", name, generated)
    return generated
