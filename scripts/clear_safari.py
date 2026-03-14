#!/usr/bin/env python3
"""Clear Safari history and website data via Settings app automation.

Usage:
    .venv/bin/python scripts/clear_safari.py --port 8100
"""
import argparse
import json
import sys
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    base = f"http://localhost:{args.port}"
    client = httpx.Client(timeout=30)

    # Create session
    resp = client.post(f"{base}/session", json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}}})
    sid = resp.json()["value"]["sessionId"]
    s = f"{base}/session/{sid}"
    print(f"Session: {sid}")

    def find(strategy, value):
        r = client.post(f"{s}/element", json={"using": strategy, "value": value})
        d = r.json()
        return d["value"].get("ELEMENT") if "ELEMENT" in d.get("value", {}) else None

    def click(eid):
        client.post(f"{s}/element/{eid}/click", json={})

    def tap(x, y):
        client.post(f"{s}/actions", json={"actions": [{"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}, "actions": [{"type": "pointerMove", "duration": 0, "x": x, "y": y}, {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 100}, {"type": "pointerUp", "button": 0}]}]})

    def swipe_up():
        client.post(f"{s}/wda/dragfromtoforduration", json={"fromX": 196, "fromY": 600, "toX": 196, "toY": 200, "duration": 0.3})

    def type_text(text):
        client.post(f"{s}/wda/keys", json={"value": list(text)})

    def screenshot(name="screen"):
        r = client.get(f"{base}/screenshot")
        import base64
        img = base64.b64decode(r.json()["value"])
        with open(f"/tmp/{name}.png", "wb") as f:
            f.write(img)
        print(f"Screenshot: /tmp/{name}.png ({len(img)} bytes)")

    def terminate(bundle):
        client.post(f"{s}/wda/apps/terminate", json={"bundleId": bundle})

    def launch(bundle):
        client.post(f"{s}/wda/apps/activate", json={"bundleId": bundle})

    # Step 1: Close Safari
    print("Closing Safari...")
    terminate("com.apple.mobilesafari")
    time.sleep(0.5)

    # Step 2: Open Safari to trigger Settings URL scheme
    print("Opening Settings via Safari URL scheme...")
    launch("com.apple.mobilesafari")
    time.sleep(1)
    client.post(f"{s}/url", json={"url": "App-prefs:SAFARI"})
    time.sleep(3)

    # Step 3: Search for Safari in the Apps list
    print("Searching for Safari in Settings...")
    search = find("predicate string", 'name CONTAINS[c] "Search Apps"')
    if not search:
        print("ERROR: Search field not found")
        screenshot("error_no_search")
        terminate("com.apple.Preferences")
        return False
    click(search)
    time.sleep(0.5)
    type_text("Safari")
    time.sleep(1)

    # Tap Search button on keyboard
    search_btn = find("predicate string", 'name == "Search" AND type == "XCUIElementTypeButton"')
    if search_btn:
        click(search_btn)
        time.sleep(1)

    # Step 4: Tap Safari
    safari = find("predicate string", 'name == "Safari"')
    if not safari:
        print("ERROR: Safari not found in search results")
        screenshot("error_no_safari")
        terminate("com.apple.Preferences")
        return False
    click(safari)
    time.sleep(2)
    print("In Safari settings")

    # Step 5: Scroll down to find "Clear History and Website Data"
    for i in range(8):
        # Try multiple match strategies
        for pred in [
            'label CONTAINS[c] "Clear History"',
            'name CONTAINS[c] "Clear History"',
            'value CONTAINS[c] "Clear History"',
        ]:
            el = find("predicate string", pred)
            if el:
                print(f"Found 'Clear History' on scroll {i}")
                click(el)
                time.sleep(2)
                screenshot("after_clear_tap")

                # Step 6: Confirm
                # iOS 18 shows a bottom sheet with timeframe selector and "Clear History" button
                for confirm_pred in [
                    'name == "Clear History" AND type == "XCUIElementTypeButton"',
                    'name == "Clear" AND type == "XCUIElementTypeButton"',
                    'label == "Clear History" AND type == "XCUIElementTypeButton"',
                    'name CONTAINS[c] "Clear" AND type == "XCUIElementTypeButton"',
                ]:
                    confirm = find("predicate string", confirm_pred)
                    if confirm:
                        click(confirm)
                        print("Confirmed clear!")
                        time.sleep(2)
                        terminate("com.apple.Preferences")
                        return True

                # Try alert
                try:
                    alert_resp = client.get(f"{s}/alert/text")
                    if alert_resp.status_code == 200:
                        client.post(f"{s}/alert/accept")
                        print("Accepted alert")
                        time.sleep(1)
                        terminate("com.apple.Preferences")
                        return True
                except Exception:
                    pass

                screenshot("confirm_failed")
                print("WARNING: Could not find confirmation button")
                terminate("com.apple.Preferences")
                return False

        swipe_up()
        time.sleep(1)

    print("ERROR: 'Clear History and Website Data' not found after scrolling")
    screenshot("error_no_clear")
    terminate("com.apple.Preferences")
    return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
