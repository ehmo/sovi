"""Direct WDA (WebDriverAgent) HTTP client — no Appium middleware needed.

WDA exposes a W3C WebDriver-compatible HTTP API. We talk to it directly
via iproxy tunnels, which is simpler and more reliable than going through
Appium for basic automation tasks like warming.
"""

from __future__ import annotations

import base64
import logging
import random
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class WDADevice:
    """A device accessible via WDA over iproxy."""

    name: str
    udid: str
    wda_port: int  # iproxy local port → device port 8100

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.wda_port}"


class WDASession:
    """Session on a single WDA device."""

    def __init__(self, device: WDADevice, timeout: float = 60.0) -> None:
        self.device = device
        self.client = httpx.Client(base_url=device.base_url, timeout=timeout)
        # Gesture client for swipe/tap — TikTok makes WDA very slow,
        # so we use 30s timeout instead of 10s
        self._gesture_client = httpx.Client(base_url=device.base_url, timeout=30.0)
        self.session_id: str | None = None
        self._screen_size: dict | None = None

    def connect(self) -> None:
        """Create a WDA session and cache screen size."""
        resp = self.client.post("/session", json={
            "capabilities": {"alwaysMatch": {"shouldWaitForQuiescence": False}}
        })
        data = resp.json()
        self.session_id = data.get("sessionId") or data.get("value", {}).get("sessionId")
        if not self.session_id:
            raise RuntimeError(f"Failed to create WDA session: {data}")
        logger.info("WDA session %s on %s", self.session_id[:8], self.device.name)
        # Eagerly cache screen size while WDA is fresh
        try:
            self.screen_size()
        except Exception:
            pass

    def disconnect(self) -> None:
        if self.session_id:
            try:
                self.client.delete(f"/session/{self.session_id}")
            except Exception:
                pass
            self.session_id = None

    @property
    def _s(self) -> str:
        """Session URL prefix."""
        return f"/session/{self.session_id}"

    # --- Status ---

    def status(self) -> dict:
        return self.client.get("/status").json()["value"]

    def is_ready(self) -> bool:
        try:
            return self.status().get("ready", False)
        except Exception:
            return False

    # --- Screen ---

    # Default iPhone 16 screen size as fallback
    _DEFAULT_SCREEN = {"width": 393, "height": 852}

    def screen_size(self) -> dict:
        if not self._screen_size:
            try:
                resp = self.client.get(f"{self._s}/window/size")
                value = resp.json().get("value", {})
                if isinstance(value, dict) and "width" in value and "height" in value:
                    self._screen_size = value
                else:
                    logger.warning("Bad screen_size response: %s, using default", str(value)[:100])
                    self._screen_size = self._DEFAULT_SCREEN.copy()
            except Exception:
                logger.warning("Error getting screen size, using default %s", self._DEFAULT_SCREEN)
                self._screen_size = self._DEFAULT_SCREEN.copy()
        return self._screen_size

    def screenshot(self, save_path: str | None = None) -> bytes:
        try:
            resp = self.client.get(f"{self._s}/screenshot")
            b64 = resp.json()["value"]
            png = base64.b64decode(b64)
            if save_path:
                with open(save_path, "wb") as f:
                    f.write(png)
            return png
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout taking screenshot")
            return b""

    # --- App management ---

    def launch_app(self, bundle_id: str) -> None:
        """Activate (bring to foreground) an app."""
        try:
            self.client.post(f"{self._s}/wda/apps/activate", json={"bundleId": bundle_id})
            logger.info("Launched %s", bundle_id)
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout launching %s (may have succeeded)", bundle_id)

    def terminate_app(self, bundle_id: str) -> None:
        try:
            self.client.post(f"{self._s}/wda/apps/terminate", json={"bundleId": bundle_id})
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout terminating %s", bundle_id)

    def app_state(self, bundle_id: str) -> int:
        """Get app state: 1=not running, 2=bg, 3=suspended, 4=foreground."""
        resp = self.client.post(f"{self._s}/wda/apps/state", json={"bundleId": bundle_id})
        return resp.json()["value"]

    def open_url(self, url: str) -> None:
        """Open a URL on the device (e.g. itms-apps:// for App Store)."""
        try:
            self.client.post(f"{self._s}/url", json={"url": url})
            logger.info("Opened URL: %s", url[:80])
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout opening URL %s (may have succeeded)", url[:80])

    # --- Element finding ---

    def find_element(self, using: str, value: str) -> dict | None:
        """Find element. using: 'accessibility id', 'predicate string', 'class chain', 'xpath'."""
        try:
            resp = self.client.post(
                f"{self._s}/element",
                json={"using": using, "value": value},
            )
            data = resp.json()
            if "value" in data and isinstance(data["value"], dict) and "ELEMENT" in data["value"]:
                return data["value"]
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.debug("Timeout finding element %s=%s", using, value)
        except Exception:
            logger.debug("Error finding element %s=%s", using, value, exc_info=True)
        return None

    def find_elements(self, using: str, value: str) -> list[dict]:
        try:
            resp = self.client.post(
                f"{self._s}/elements",
                json={"using": using, "value": value},
            )
            return resp.json().get("value", [])
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.debug("Timeout finding elements %s=%s", using, value)
            return []

    def element_click(self, element_id: str) -> None:
        try:
            self.client.post(f"{self._s}/element/{element_id}/click")
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout on element_click (action may have succeeded)")

    def element_value(self, element_id: str, text: str) -> None:
        """Type into an element."""
        self.client.post(f"{self._s}/element/{element_id}/value", json={"value": list(text)})

    def element_clear(self, element_id: str) -> None:
        """Clear an element's text content."""
        self.client.post(f"{self._s}/element/{element_id}/clear")

    def drag(
        self, from_x: int, from_y: int, to_x: int, to_y: int,
        duration: float = 0.5, timeout: float | None = None,
    ) -> None:
        """Drag gesture with optional custom timeout.

        Uses WDA's dragfromtoforduration. The gesture executes immediately
        but TikTok's quiescence issues cause the HTTP response to take ~57s.
        Use a short timeout (e.g. 5s) when you know the gesture will execute
        faster than the response arrives.
        """
        client = self._gesture_client
        if timeout is not None:
            client = httpx.Client(base_url=self.device.base_url, timeout=timeout)
        try:
            client.post(
                f"{self._s}/wda/dragfromtoforduration",
                json={
                    "fromX": from_x, "fromY": from_y,
                    "toX": to_x, "toY": to_y,
                    "duration": duration,
                },
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.debug("Timeout on drag — gesture likely executed")
        finally:
            if timeout is not None:
                client.close()

    # --- Touch / gestures ---

    def tap(self, x: int, y: int, duration: int = 500) -> None:
        """Tap at coordinates using W3C actions.

        Args:
            duration: press hold time in ms. TikTok's custom views need >=500ms
                      to register a tap; shorter durations are silently ignored.
        """
        try:
            self._gesture_client.post(f"{self._s}/actions", json={
                "actions": [{
                    "type": "pointer",
                    "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pause", "duration": duration},
                        {"type": "pointerUp", "button": 0},
                    ],
                }],
            })
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout on tap(%d, %d) — gesture likely executed", x, y)

    def double_tap(self, x: int, y: int) -> None:
        try:
            self._gesture_client.post(f"{self._s}/actions", json={
                "actions": [{
                    "type": "pointer",
                    "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerUp", "button": 0},
                        {"type": "pause", "duration": 40},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerUp", "button": 0},
                    ],
                }],
            })
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout on double_tap(%d, %d) — gesture likely executed", x, y)

    def swipe(self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.5) -> None:
        """Swipe gesture."""
        try:
            self._gesture_client.post(
                f"{self._s}/wda/dragfromtoforduration",
                json={
                    "fromX": start_x,
                    "fromY": start_y,
                    "toX": end_x,
                    "toY": end_y,
                    "duration": duration,
                },
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout on swipe — gesture likely executed")

    def swipe_up(self, duration: float = 0.5) -> None:
        """Swipe up (scroll down / next video on TikTok)."""
        size = self.screen_size()
        cx = size["width"] // 2
        self.swipe(cx, int(size["height"] * 0.75), cx, int(size["height"] * 0.25), duration)

    def swipe_down(self, duration: float = 0.5) -> None:
        """Swipe down (scroll up)."""
        size = self.screen_size()
        cx = size["width"] // 2
        self.swipe(cx, int(size["height"] * 0.25), cx, int(size["height"] * 0.75), duration)

    # --- Alerts ---

    def get_alert_text(self) -> str | None:
        try:
            resp = self._gesture_client.get(f"{self._s}/alert/text")
            data = resp.json()
            value = data.get("value")
            if isinstance(value, dict) and "error" in value:
                return None
            return value
        except Exception:
            return None

    def accept_alert(self) -> bool:
        try:
            self._gesture_client.post(f"{self._s}/alert/accept")
            return True
        except Exception:
            return False

    def dismiss_alert(self) -> bool:
        try:
            self._gesture_client.post(f"{self._s}/alert/dismiss")
            return True
        except Exception:
            return False

    # --- Page source ---

    def source(self) -> str:
        resp = self.client.get(f"{self._s}/source")
        return resp.json()["value"]

    # --- Keyboard ---

    def type_text(self, text: str) -> None:
        """Type text via WDA keyboard (must have keyboard visible / field focused)."""
        try:
            self.client.post(f"{self._s}/wda/keys", json={"value": list(text)})
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout typing text (may have succeeded)")

    def press_button(self, name: str) -> None:
        """Press a hardware button: 'home', 'volumeUp', 'volumeDown'."""
        self.client.post(f"{self._s}/wda/pressButton", json={"name": name})

    # --- Safari navigation ---

    def navigate_to(self, url: str) -> None:
        """Navigate current browser page to URL."""
        try:
            self.client.post(f"{self._s}/url", json={"url": url})
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout navigating to %s (may have succeeded)", url)

    def open_safari(self, url: str) -> None:
        """Launch Safari and navigate to URL."""
        self.launch_app("com.apple.mobilesafari")
        time.sleep(2)
        self.navigate_to(url)

    def close_safari(self) -> None:
        """Close Safari cleanly."""
        self.terminate_app("com.apple.mobilesafari")

    # --- Device reset (shared by scheduler + seeder) ---

    def reset_to_home(self) -> None:
        """Return device to a clean home screen state after a task.

        Terminates common apps that may have been left open (App Store,
        Safari, social apps) and presses Home twice to ensure we're on
        the springboard. Swallows all errors — this is best-effort recovery.
        """
        for bundle in ("com.apple.AppStore", "com.apple.mobilesafari",
                        "com.zhiliaoapp.musically", "com.burbn.instagram"):
            try:
                self.terminate_app(bundle)
            except Exception:
                pass
        try:
            self.press_button("home")
            time.sleep(0.5)
            self.press_button("home")
        except Exception:
            pass

    # --- WiFi enforcement (must be OFF — all traffic via cellular) ---

    def ensure_wifi_off(self) -> bool:
        """Ensure WiFi is disabled via Control Center.

        ALL persona-facing traffic MUST go through cellular/GSM.
        WiFi must never be active during tasks. This opens Control Center,
        checks the WiFi button state, and disables it if active.

        Returns True if WiFi is confirmed off.
        """
        size = self.screen_size()
        w, h = size["width"], size["height"]

        try:
            # Open Control Center
            self.swipe(int(w * 0.9), 0, int(w * 0.5), int(h * 0.5), duration=0.3)
            time.sleep(1.5)

            # Find WiFi button
            wifi_el = self.find_element("accessibility id", "wifi-button")
            if wifi_el:
                # Check if WiFi is enabled — the label/value contains state info
                # If the button's value indicates it's on, tap to disable
                el_id = wifi_el["ELEMENT"]
                try:
                    resp = self.client.get(f"{self._s}/element/{el_id}/attribute/value", timeout=5)
                    value = resp.json().get("value", "")
                except Exception:
                    value = ""

                # WiFi button value is "Wi-Fi" when off, "Wi-Fi, <network>" when on
                if "," in str(value) or "connected" in str(value).lower():
                    self.element_click(el_id)
                    logger.info("WiFi was ON — disabled on %s", self.device.name)
                    time.sleep(1.0)
                else:
                    logger.info("WiFi confirmed off on %s", self.device.name)
            else:
                # Fallback: tap the known WiFi coordinate area
                # WiFi is next to airplane mode in the connectivity group
                self.tap(int(w * 0.35), int(h * 0.18))
                logger.warning("WiFi button not found by accessibility id, used coordinate tap on %s", self.device.name)
                time.sleep(1.0)

                # Re-check WiFi status after fallback tap
                wifi_el_recheck = self.find_element("accessibility id", "wifi-button")
                if wifi_el_recheck:
                    el_id_recheck = wifi_el_recheck["ELEMENT"]
                    try:
                        resp = self.client.get(f"{self._s}/element/{el_id_recheck}/attribute/value", timeout=5)
                        value_recheck = resp.json().get("value", "")
                    except Exception:
                        value_recheck = ""

                    if "," in str(value_recheck) or "connected" in str(value_recheck).lower():
                        logger.warning("WiFi still ON after fallback tap on %s — could not verify off", self.device.name)
                        self.swipe(int(w * 0.5), int(h * 0.9), int(w * 0.5), int(h * 0.5), duration=0.3)
                        time.sleep(0.5)
                        return False
                else:
                    logger.warning("Cannot verify WiFi state after fallback tap on %s", self.device.name)
                    self.swipe(int(w * 0.5), int(h * 0.9), int(w * 0.5), int(h * 0.5), duration=0.3)
                    time.sleep(0.5)
                    return False

            # Close Control Center
            self.swipe(int(w * 0.5), int(h * 0.9), int(w * 0.5), int(h * 0.5), duration=0.3)
            time.sleep(0.5)
            return True

        except Exception:
            logger.error("Failed to ensure WiFi off on %s", self.device.name, exc_info=True)
            try:
                self.press_button("home")
            except Exception:
                pass
            return False

    # --- Airplane mode (IP rotation) ---

    def toggle_airplane_mode(self, wait_after: float = 6.0) -> bool:
        """Toggle airplane mode ON then OFF via Control Center to rotate cellular IP.

        Swipes down from top-right to open Control Center, taps the airplane
        mode icon, waits, then taps again to re-enable cellular.

        Returns True if the toggle sequence completed without error.
        """
        size = self.screen_size()
        w, h = size["width"], size["height"]

        try:
            # Open Control Center — swipe down from top-right corner
            self.swipe(int(w * 0.9), 0, int(w * 0.5), int(h * 0.5), duration=0.3)
            time.sleep(1.5)

            # Airplane mode button is in the top-left connectivity group
            # Tap it to enable (cuts cellular + wifi)
            airplane_el = self.find_element("accessibility id", "airplane-mode-button")
            if airplane_el:
                self.element_click(airplane_el["ELEMENT"])
            else:
                # Fallback: tap the known coordinate area for airplane mode
                # Control Center connectivity group top-left quadrant
                self.tap(int(w * 0.18), int(h * 0.18))
            time.sleep(3.0)

            # Tap again to disable airplane mode (cellular reconnects)
            # Retry up to 3 times to ensure airplane mode is OFF
            for attempt in range(3):
                airplane_el = self.find_element("accessibility id", "airplane-mode-button")
                if airplane_el:
                    el_id = airplane_el["ELEMENT"]
                    # Check if still enabled (value contains "1" or "On")
                    attrs = self.wda.get(
                        f"/session/{self.session_id}/element/{el_id}/attribute/value"
                    ).json().get("value", "")
                    if attrs and ("1" in str(attrs) or "On" in str(attrs)):
                        self.element_click(el_id)
                        time.sleep(2.0)
                        continue  # re-check
                    else:
                        break  # airplane mode is off
                else:
                    self.tap(int(w * 0.18), int(h * 0.18))
                    time.sleep(2.0)
                    break

            # Close Control Center — swipe up from bottom or tap empty area
            time.sleep(0.5)
            self.swipe(int(w * 0.5), int(h * 0.9), int(w * 0.5), int(h * 0.5), duration=0.3)
            time.sleep(0.5)

            # Wait for cellular to reconnect
            time.sleep(wait_after)

            logger.info("Airplane mode toggled on %s — IP rotated", self.device.name)
            return True

        except Exception:
            logger.error("Failed to toggle airplane mode on %s", self.device.name, exc_info=True)
            # CRITICAL: try to ensure airplane mode is OFF before giving up
            try:
                airplane_el = self.find_element("accessibility id", "airplane-mode-button")
                if airplane_el:
                    attrs = self.wda.get(
                        f"/session/{self.session_id}/element/{airplane_el['ELEMENT']}/attribute/value"
                    ).json().get("value", "")
                    if attrs and ("1" in str(attrs) or "On" in str(attrs)):
                        self.element_click(airplane_el["ELEMENT"])
                        logger.info("Emergency airplane mode OFF on %s", self.device.name)
                self.press_button("home")
            except Exception:
                pass
            return False


# --- Bundle IDs (canonical map — import from here) ---

BUNDLE_IDS: dict[str, str] = {
    "tiktok": "com.zhiliaoapp.musically",
    "instagram": "com.burbn.instagram",
    "youtube": "com.google.ios.youtube",
    "youtube_shorts": "com.google.ios.youtube",
    "reddit": "com.reddit.Reddit",
    "twitter": "com.atebits.Tweetie2",
    "x_twitter": "com.atebits.Tweetie2",
    "facebook": "com.facebook.Facebook",
    "linkedin": "com.linkedin.LinkedIn",
    "safari": "com.apple.mobilesafari",
}


# --- High-level automation helpers ---


class DeviceAutomation:
    """High-level automation actions using WDA directly."""

    def __init__(self, session: WDASession) -> None:
        self.wda = session

    def human_delay(self, min_s: float = 0.3, max_s: float = 1.5) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def launch(self, app_name: str) -> None:
        bundle_id = BUNDLE_IDS.get(app_name, app_name)
        self.wda.launch_app(bundle_id)
        time.sleep(random.uniform(2.5, 4.5))
        self.dismiss_popups()

    def dismiss_popups(self, max_attempts: int = 3) -> int:
        """Try to dismiss system alerts and in-app popups."""
        dismissed = 0
        for _ in range(max_attempts):
            # System alert
            alert_text = self.wda.get_alert_text()
            if alert_text:
                alert_str = str(alert_text) if not isinstance(alert_text, str) else alert_text
                logger.info("Alert: %s", alert_str[:80])
                # Reject WiFi-related alerts — devices must stay cellular-only
                alert_lower = alert_str.lower()
                if any(kw in alert_lower for kw in ["wi-fi", "wifi", "wireless", "network"]):
                    self.wda.dismiss_alert()  # "Don't Allow" / "Cancel" for WiFi
                # Dismiss tracking/notifications (we want normal behavior)
                elif any(kw in alert_lower for kw in ["allow", "notif", "track"]):
                    self.wda.dismiss_alert()  # "Don't Allow" for tracking
                else:
                    self.wda.accept_alert()
                dismissed += 1
                time.sleep(0.5)
                continue

            # In-app dismiss buttons
            for label in ["Not Now", "Skip", "Later", "Got it", "Dismiss", "Close", "No thanks"]:
                el = self.wda.find_element("accessibility id", label)
                if el:
                    el_id = el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                        dismissed += 1
                        logger.info("Dismissed: %s", label)
                        time.sleep(0.5)
                        break
            else:
                break
        return dismissed

    def like_current(self) -> None:
        """Double-tap center of screen to like."""
        size = self.wda.screen_size()
        cx, cy = size["width"] // 2, size["height"] // 2
        self.wda.double_tap(cx, cy)
        self.human_delay(0.5, 1.5)

    def tap_element(self, using: str, value: str) -> bool:
        """Find and tap an element. Returns True if found."""
        el = self.wda.find_element(using, value)
        if el:
            el_id = el.get("ELEMENT", "")
            if el_id:
                self.wda.element_click(el_id)
                return True
        return False
