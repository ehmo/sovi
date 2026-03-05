"""Proxy assignment and health checking for device identity isolation.

Each device gets 1 static proxy. Proxies are inserted manually into the DB.
Network routing is configured at the iPhone Wi-Fi settings level.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from sovi.crypto import decrypt
from sovi.db import sync_execute, sync_execute_one

logger = logging.getLogger(__name__)


def get_device_proxy(device_id: str) -> dict[str, Any] | None:
    """Get the proxy assigned to a device."""
    return sync_execute_one(
        """SELECT id, provider, type, host, port, credentials_enc,
                  geo_country, geo_region, is_healthy, last_health_check
           FROM proxies
           WHERE assigned_device_id = %s""",
        (device_id,),
    )


def assign_proxy_to_device(proxy_id: str, device_id: str) -> bool:
    """Assign a proxy to a device (sets FK both ways, single transaction)."""
    try:
        from sovi.db import sync_conn
        with sync_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE proxies SET assigned_device_id = %s, updated_at = now() WHERE id = %s",
                    (device_id, proxy_id),
                )
                cur.execute(
                    "UPDATE devices SET current_proxy_id = %s, updated_at = now() WHERE id = %s",
                    (proxy_id, device_id),
                )
            conn.commit()
        logger.info("Assigned proxy %s to device %s", proxy_id[:8], device_id[:8])
        return True
    except Exception:
        logger.error("Failed to assign proxy %s to device %s",
                     proxy_id[:8], device_id[:8], exc_info=True)
        return False


def proxy_url(proxy: dict[str, Any]) -> str:
    """Build socks5://user:pass@host:port from proxy row."""
    host = proxy["host"]
    port = proxy["port"]

    if proxy.get("credentials_enc"):
        creds = decrypt(proxy["credentials_enc"])
        # Expect "user:pass" format
        return f"socks5://{creds}@{host}:{port}"

    return f"socks5://{host}:{port}"


def health_check_proxy(proxy: dict[str, Any]) -> bool:
    """Check proxy health by fetching api.ipify.org through it.

    Updates is_healthy and last_health_check in DB.
    """
    proxy_id = str(proxy["id"])
    url = proxy_url(proxy)

    try:
        resp = httpx.get(
            "https://api.ipify.org?format=json",
            proxy=url,
            timeout=15.0,
        )
        healthy = resp.status_code == 200
        ip = resp.json().get("ip", "unknown") if healthy else None

        sync_execute(
            """UPDATE proxies
               SET is_healthy = %s, last_health_check = now(), updated_at = now()
               WHERE id = %s""",
            (healthy, proxy_id),
        )

        if healthy:
            logger.info("Proxy %s healthy (IP: %s)", proxy_id[:8], ip)
        else:
            logger.warning("Proxy %s unhealthy (status %d)", proxy_id[:8], resp.status_code)

        return healthy

    except Exception:
        logger.error("Proxy %s health check failed", proxy_id[:8], exc_info=True)
        sync_execute(
            """UPDATE proxies
               SET is_healthy = false, last_health_check = now(), updated_at = now()
               WHERE id = %s""",
            (proxy_id,),
        )
        return False
