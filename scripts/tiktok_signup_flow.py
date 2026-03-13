#!/usr/bin/env python3
"""TikTok signup flow — standalone test runner.

Uses the sovi WDA client and account_creator directly.
Run with: .venv/bin/python scripts/tiktok_signup_flow.py [wda_port] [email]

Set SOVI_SIGNUP_DEBUG=1 to save screenshots to /tmp/sovi_signup/.

Coordinate reference (iPhone 16, 393x852 points):
    Login screen:
        "Sign up" link: (280, 799)
    Signup method screen:
        "Use phone or email": red button ~(196, 243)
    Birthday screen:
        Month: (137, 654), Day: (280, 654), Year: (357, 654)
        "Continue": (197, 770)
    Email/Phone screen:
        "Email" tab: (290, 130)
        Email input: (196, 220)
"""

import logging
import os
import sys
import time

# Enable debug screenshots
os.environ["SOVI_SIGNUP_DEBUG"] = "1"

# Add src to path for direct execution
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sovi.device.wda_client import WDADevice, WDASession, DeviceAutomation
from sovi.device.account_creator import _signup_tiktok

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    email = sys.argv[2] if len(sys.argv) > 2 else "test@example.com"
    password = "TestP@ss123!"

    print(f"TikTok signup test on port {port}")
    print(f"Email: {email}")
    print(f"Screenshots: /tmp/sovi_signup/")

    device = WDADevice(name="test", udid="test", wda_port=port)
    session = WDASession(device, timeout=120.0)
    session.connect()
    auto = DeviceAutomation(session)

    try:
        result = _signup_tiktok(
            session, auto,
            email=email,
            password=password,
            username="testuser123",
            imap_config=None,
            device_id="test-device",
        )
        print(f"\nResult: {'SUCCESS' if result else 'FAILED'}")
    finally:
        session.disconnect()


if __name__ == "__main__":
    main()
