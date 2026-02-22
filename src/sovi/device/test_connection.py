"""Quick test: verify Appium can connect to both iPhones via WDA.

Run on Mac Studio:
    python -m sovi.device.test_connection
"""

from __future__ import annotations

import json
import sys

import httpx


def test_wda_direct(port: int, name: str) -> bool:
    """Test WDA HTTP endpoint directly (no Appium needed)."""
    try:
        resp = httpx.get(f"http://localhost:{port}/status", timeout=5)
        data = resp.json()
        v = data["value"]
        print(f"  {name} (port {port}):")
        print(f"    Ready: {v['ready']}")
        print(f"    iOS: {v['os']['version']}")
        print(f"    IP: {v['ios']['ip']}")
        print(f"    WDA: {v['build']['version']}")
        return v["ready"]
    except Exception as e:
        print(f"  {name} (port {port}): FAILED - {e}")
        return False


def test_wda_session(port: int, name: str) -> bool:
    """Create a WDA session directly (minimal Appium-like call)."""
    try:
        resp = httpx.post(
            f"http://localhost:{port}/session",
            json={"capabilities": {"alwaysMatch": {}}},
            timeout=15,
        )
        data = resp.json()
        session_id = data.get("sessionId") or data.get("value", {}).get("sessionId")
        if session_id:
            print(f"  {name}: Session created ({session_id[:8]}...)")

            # Get screen info
            resp2 = httpx.get(f"http://localhost:{port}/session/{session_id}/window/size", timeout=5)
            size = resp2.json().get("value", {})
            print(f"  {name}: Screen {size.get('width')}x{size.get('height')}")

            # Get source tree (just top level)
            resp3 = httpx.get(f"http://localhost:{port}/session/{session_id}/source", timeout=10)
            source = resp3.text[:200]
            print(f"  {name}: Source tree starts with: {source[:100]}...")

            # Take screenshot
            resp4 = httpx.get(f"http://localhost:{port}/session/{session_id}/screenshot", timeout=10)
            ss = resp4.json().get("value", "")
            if ss:
                print(f"  {name}: Screenshot OK ({len(ss)} base64 chars)")

            # Delete session
            httpx.delete(f"http://localhost:{port}/session/{session_id}", timeout=5)
            return True
        else:
            print(f"  {name}: No session ID in response: {json.dumps(data)[:200]}")
            return False
    except Exception as e:
        print(f"  {name}: Session test FAILED - {e}")
        return False


def main() -> None:
    devices = [
        (8100, "iPhone-A (iOS 26.2)"),
        (8101, "iPhone-B (iOS 26.1)"),
    ]

    print("=== WDA Status Check ===")
    all_ok = True
    for port, name in devices:
        if not test_wda_direct(port, name):
            all_ok = False

    print("\n=== WDA Session Test ===")
    for port, name in devices:
        if not test_wda_session(port, name):
            all_ok = False

    if all_ok:
        print("\nAll devices ready for automation!")
        sys.exit(0)
    else:
        print("\nSome devices failed. Check WDA and iproxy services.")
        sys.exit(1)


if __name__ == "__main__":
    main()
