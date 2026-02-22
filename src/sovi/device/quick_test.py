"""Quick smoke test: launch TikTok on iPhone B via direct WDA, verify automation works.

Run on Mac Studio:
    python -m sovi.device.quick_test
"""

from __future__ import annotations

import time

from sovi.device.wda_client import WDADevice, WDASession, DeviceAutomation


def main() -> None:
    devices = [
        WDADevice(name="iPhone-A", udid="00008140-001975DC3678801C", wda_port=8100),
        WDADevice(name="iPhone-B", udid="00008140-001A00141163001C", wda_port=8101),
    ]

    # Test iPhone B (has all social apps)
    device = devices[1]
    print(f"=== Testing {device.name} (port {device.wda_port}) ===")

    session = WDASession(device)
    session.connect()
    print(f"Session: {session.session_id[:8]}...")

    auto = DeviceAutomation(session)

    try:
        # Screen info
        size = session.screen_size()
        print(f"Screen: {size['width']}x{size['height']}")

        # Home screenshot
        session.screenshot("/tmp/sovi-test-home.png")
        print("Home screenshot saved")

        # Launch TikTok
        print("\nLaunching TikTok...")
        auto.launch("tiktok")
        time.sleep(3)

        session.screenshot("/tmp/sovi-test-tiktok.png")
        print("TikTok screenshot saved")

        # Swipe to next video
        print("Swiping to next video...")
        session.swipe_up(duration=0.4)
        time.sleep(3)

        session.screenshot("/tmp/sovi-test-tiktok-swiped.png")
        print("Swiped screenshot saved")

        # Swipe through a few more
        for i in range(3):
            time.sleep(2)
            session.swipe_up(duration=0.4)
            print(f"  Swipe {i+2} done")

        # Launch Instagram
        print("\nLaunching Instagram...")
        auto.launch("instagram")
        time.sleep(3)

        session.screenshot("/tmp/sovi-test-instagram.png")
        print("Instagram screenshot saved")

        # Swipe through feed
        for i in range(3):
            time.sleep(2)
            session.swipe_up(duration=0.5)
            print(f"  Feed scroll {i+1}")

        print("\n=== All tests passed! Device automation is working. ===")

    finally:
        session.disconnect()
        print("Session closed.")


if __name__ == "__main__":
    main()
