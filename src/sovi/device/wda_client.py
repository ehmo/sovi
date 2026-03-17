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
from typing import Any

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

    @staticmethod
    def _response_has_invalid_session(payload: Any) -> bool:
        """Detect WDA's invalid-session payloads across endpoint shapes."""
        text = str(payload).lower()
        return "invalid session id" in text or "session does not exist" in text

    def screen_size(self) -> dict:
        if not self._screen_size:
            try:
                resp = self.client.get(f"{self._s}/window/size")
                data = resp.json()
                if self._response_has_invalid_session(data):
                    raise RuntimeError("invalid session id")
                value = data.get("value", {})
                if self._response_has_invalid_session(value):
                    raise RuntimeError("invalid session id")
                if isinstance(value, dict) and "width" in value and "height" in value:
                    self._screen_size = value
                else:
                    logger.warning("Bad screen_size response: %s, using default", str(value)[:100])
                    self._screen_size = self._DEFAULT_SCREEN.copy()
            except RuntimeError:
                raise
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
        data = resp.json()
        value = data.get("value")
        if self._response_has_invalid_session(data) or self._response_has_invalid_session(value):
            raise RuntimeError("invalid session id")
        return value

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
            if self._response_has_invalid_session(data) or self._response_has_invalid_session(data.get("value")):
                raise RuntimeError("invalid session id")
            if "value" in data and isinstance(data["value"], dict) and "ELEMENT" in data["value"]:
                return data["value"]
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.debug("Timeout finding element %s=%s", using, value)
        except RuntimeError:
            raise
        except Exception:
            logger.debug("Error finding element %s=%s", using, value, exc_info=True)
        return None

    def find_elements(self, using: str, value: str) -> list[dict]:
        try:
            resp = self.client.post(
                f"{self._s}/elements",
                json={"using": using, "value": value},
            )
            data = resp.json()
            if self._response_has_invalid_session(data) or self._response_has_invalid_session(data.get("value")):
                raise RuntimeError("invalid session id")
            return data.get("value", [])
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.debug("Timeout finding elements %s=%s", using, value)
            return []
        except RuntimeError:
            raise

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
        data = resp.json()
        value = data.get("value")
        if self._response_has_invalid_session(data) or self._response_has_invalid_session(value):
            raise RuntimeError("invalid session id")
        return value

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

    # --- Network state enforcement (must be cellular-only) ---

    def _stabilize_home_for_system_gesture(self) -> None:
        """Return to the home screen before sending Control Center gestures."""
        try:
            self.press_button("home")
            time.sleep(0.4)
            self.press_button("home")
            time.sleep(0.8)
        except Exception:
            pass

    def _control_center_is_visible(self) -> bool:
        """Best-effort check that Control Center is actually open."""
        return bool(
            self._find_control_center_toggle("airplane")
            or self._find_control_center_toggle("wifi")
        )

    def _open_control_center(self) -> bool:
        """Open Control Center from the top-right corner and verify it opened."""
        size = self.screen_size()
        w, h = size["width"], size["height"]
        gestures = (
            (0.97, 0.03, 0.74, 0.38, 0.18),
            (0.94, 0.05, 0.68, 0.48, 0.24),
            (0.98, 0.02, 0.58, 0.58, 0.30),
        )

        for start_x, start_y, end_x, end_y, duration in gestures:
            self._stabilize_home_for_system_gesture()
            self.swipe(
                int(w * start_x),
                max(1, int(h * start_y)),
                int(w * end_x),
                int(h * end_y),
                duration=duration,
            )
            time.sleep(1.2)
            try:
                if self._control_center_is_visible():
                    return True
            except RuntimeError:
                if self.reconnect(attempts=1, delay_s=0.5):
                    continue
                raise

        logger.warning("Could not open Control Center on %s", self.device.name)
        return False

    def _close_control_center(self) -> None:
        """Dismiss Control Center."""
        try:
            self.press_button("home")
            time.sleep(0.5)
        except Exception:
            size = self.screen_size()
            w, h = size["width"], size["height"]
            self.swipe(int(w * 0.5), int(h * 0.9), int(w * 0.5), int(h * 0.5), duration=0.3)
            time.sleep(0.5)

    def _get_element_attribute(self, element_id: str, attribute: str) -> Any:
        """Fetch a single element attribute from WDA."""
        try:
            resp = self.client.get(
                f"{self._s}/element/{element_id}/attribute/{attribute}",
                timeout=5,
            )
            return resp.json().get("value")
        except Exception:
            return None

    def element_attribute(self, element_id: str, attribute: str) -> Any:
        """Public attribute accessor for signup/login flows."""
        return self._get_element_attribute(element_id, attribute)

    @staticmethod
    def _coerce_bool(value: Any) -> bool | None:
        """Best-effort bool coercion for WDA attribute values."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if value is None:
            return None

        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "selected"}:
            return True
        if text in {"0", "false", "no", "off", "unselected"}:
            return False
        return None

    def _find_control_center_toggle(self, toggle: str) -> dict | None:
        """Find a Control Center network toggle by stable id, then by label."""
        if toggle == "wifi":
            wifi_switch = (
                'type == "XCUIElementTypeSwitch" AND '
                '(name CONTAINS[c] "Wi-Fi" OR label CONTAINS[c] "Wi-Fi" '
                'OR name CONTAINS[c] "wifi" OR label CONTAINS[c] "wifi")'
            )
            wifi_button = (
                'type == "XCUIElementTypeButton" AND '
                '(name CONTAINS[c] "Wi-Fi" OR label CONTAINS[c] "Wi-Fi" '
                'OR name CONTAINS[c] "wifi" OR label CONTAINS[c] "wifi")'
            )
            candidates = [
                ("accessibility id", "wifi-button"),
                ("predicate string", wifi_switch),
                ("predicate string", wifi_button),
            ]
        elif toggle == "airplane":
            airplane_switch = (
                'type == "XCUIElementTypeSwitch" AND '
                '(name CONTAINS[c] "Airplane" OR label CONTAINS[c] "Airplane")'
            )
            airplane_button = (
                'type == "XCUIElementTypeButton" AND '
                '(name CONTAINS[c] "Airplane" OR label CONTAINS[c] "Airplane")'
            )
            candidates = [
                ("accessibility id", "airplane-mode-button"),
                ("predicate string", airplane_switch),
                ("predicate string", airplane_button),
            ]
        else:
            raise ValueError(f"Unknown toggle {toggle}")

        for using, value in candidates:
            element = self.find_element(using, value)
            if element:
                return element
        return None

    def _toggle_state_from_attributes(self, toggle: str, attrs: dict[str, Any]) -> bool | None:
        """Infer a Control Center toggle state from multiple WDA attributes."""
        values = [attrs.get(attr) for attr in ("value", "label", "name")]
        normalized = [str(value).strip().lower() for value in values if value not in (None, "")]

        for value in values:
            coerced = self._coerce_bool(value)
            if coerced is not None:
                return coerced

        if toggle == "wifi":
            if any("connected" in value or "," in value for value in normalized):
                return True
            if any(value in {"wi-fi", "wifi"} or "off" in value for value in normalized):
                return False
        elif toggle == "airplane":
            if any(
                ("airplane" in value and "on" in value) or value == "on"
                for value in normalized
            ):
                return True
            if any(
                ("airplane" in value and "off" in value)
                or value in {"airplane", "airplane mode"}
                for value in normalized
            ):
                return False

        selected = self._coerce_bool(attrs.get("selected"))
        if selected is not None:
            return selected

        return None

    def _read_control_center_toggle_state(self, toggle: str) -> tuple[dict | None, bool | None]:
        """Return the toggle element and inferred state."""
        element = self._find_control_center_toggle(toggle)
        if not element:
            return None, None

        element_id = element["ELEMENT"]
        attrs = {
            attr: self._get_element_attribute(element_id, attr)
            for attr in ("value", "label", "name", "selected")
        }
        return element, self._toggle_state_from_attributes(toggle, attrs)

    def _set_control_center_toggle(self, toggle: str, *, desired_on: bool, attempts: int = 3) -> bool:
        """Set a Control Center toggle to a desired state and verify it."""
        for _ in range(attempts):
            try:
                element, state = self._read_control_center_toggle_state(toggle)
            except RuntimeError:
                if not self.reconnect(attempts=1, delay_s=0.5):
                    raise
                if not self._open_control_center():
                    logger.warning("Could not find %s toggle on %s", toggle, self.device.name)
                    return False
                element, state = self._read_control_center_toggle_state(toggle)
            if state is desired_on:
                return True
            if not element:
                if not self._open_control_center():
                    logger.warning("Could not find %s toggle on %s", toggle, self.device.name)
                    return False
                element, state = self._read_control_center_toggle_state(toggle)
                if state is desired_on:
                    return True
                if not element:
                    logger.warning("Could not find %s toggle on %s", toggle, self.device.name)
                    return False

            self.element_click(element["ELEMENT"])
            time.sleep(1.0)

        _, final_state = self._read_control_center_toggle_state(toggle)
        return final_state is desired_on

    def reconnect(self, attempts: int = 3, delay_s: float = 1.5) -> bool:
        """Recreate the WDA session after radio or app handoff churn."""
        self._screen_size = None
        for _ in range(attempts):
            try:
                self.disconnect()
            except Exception:
                pass
            try:
                self.connect()
                self.screen_size()
                return True
            except Exception:
                time.sleep(delay_s)
        return False

    def ensure_airplane_mode_off(self) -> bool:
        """Ensure airplane mode is disabled before any network activity."""
        opened = False
        try:
            opened = self._open_control_center()
            if not opened:
                return False
            ok = self._set_control_center_toggle("airplane", desired_on=False)
            if ok:
                logger.info("Airplane mode confirmed off on %s", self.device.name)
            else:
                logger.warning("Could not verify airplane mode off on %s", self.device.name)
            return ok
        except Exception:
            logger.error("Failed to ensure airplane mode off on %s", self.device.name, exc_info=True)
            return False
        finally:
            try:
                if opened:
                    self._close_control_center()
            except Exception:
                try:
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
        opened = False
        try:
            opened = self._open_control_center()
            if not opened:
                return False
            ok = self._set_control_center_toggle("wifi", desired_on=False)
            if ok:
                logger.info("WiFi confirmed off on %s", self.device.name)
            else:
                logger.warning("Could not verify WiFi off on %s", self.device.name)
            return ok
        except Exception:
            logger.error("Failed to ensure WiFi off on %s", self.device.name, exc_info=True)
            return False
        finally:
            try:
                if opened:
                    self._close_control_center()
            except Exception:
                try:
                    self.press_button("home")
                except Exception:
                    pass

    def ensure_cellular_only(self) -> bool:
        """Force the device into the expected network state: airplane off, Wi-Fi off."""
        if not self.ensure_airplane_mode_off():
            return False
        return self.ensure_wifi_off()

    # --- Airplane mode (IP rotation) ---

    def toggle_airplane_mode(self, wait_after: float = 6.0) -> bool:
        """Toggle airplane mode ON then OFF via Control Center to rotate cellular IP.

        Swipes down from top-right to open Control Center, taps the airplane
        mode icon, waits, then taps again to re-enable cellular.

        Returns True if the toggle sequence completed without error.
        """
        if not self.ensure_airplane_mode_off():
            logger.warning(
                "Refusing airplane toggle on %s because baseline airplane-off state could not be verified",
                self.device.name,
            )
            return False

        opened = False
        rotated = False
        try:
            opened = self._open_control_center()
            if not opened:
                return False
            if not self._set_control_center_toggle("airplane", desired_on=True):
                logger.warning("Could not verify airplane mode enabled on %s", self.device.name)
                return False
            time.sleep(3.0)
            if not self._set_control_center_toggle("airplane", desired_on=False):
                logger.warning("Could not verify airplane mode disabled on %s", self.device.name)
                return False
            rotated = True
        except Exception:
            logger.error("Failed to toggle airplane mode on %s", self.device.name, exc_info=True)
        finally:
            try:
                if opened:
                    self._close_control_center()
            except Exception:
                try:
                    self.press_button("home")
                except Exception:
                    pass

        time.sleep(wait_after)
        if not self.reconnect():
            logger.warning("Could not reconnect WDA session on %s after airplane toggle", self.device.name)
            return False
        if not self.ensure_cellular_only():
            logger.warning(
                "Airplane toggle on %s did not return to cellular-only state",
                self.device.name,
            )
            return False

        self.reset_to_home()
        if rotated:
            logger.info("Airplane mode toggled on %s — IP rotated", self.device.name)
        return rotated


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
