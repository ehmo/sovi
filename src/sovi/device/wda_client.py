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

    _CARRIER_PROBE_URL = "http://captive.apple.com/hotspot-detect.html"
    _CARRIER_PROBE_BACKUP_URLS = (
        "http://www.google.com/generate_204",
        "http://clients1.google.com/generate_204",
        "http://www.msftconnecttest.com/connecttest.txt",
        "http://httpbin.org/get",
    )
    _CARRIER_PROBE_SUCCESS_MARKERS = (
        "<title>success",
        "<body>success",
        ">success<",
    )
    _CARRIER_PROBE_FAILURE_MARKERS = (
        "cannot open page",
        "not connected to the internet",
        "server stopped responding",
        "offline",
    )

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
        resp = self.client.post(
            "/session", json={"capabilities": {"alwaysMatch": {"shouldWaitForQuiescence": False}}}
        )
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
            if self._response_has_invalid_session(data) or self._response_has_invalid_session(
                data.get("value")
            ):
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
            if self._response_has_invalid_session(data) or self._response_has_invalid_session(
                data.get("value")
            ):
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
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration: float = 0.5,
        timeout: float | None = None,
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
                    "fromX": from_x,
                    "fromY": from_y,
                    "toX": to_x,
                    "toY": to_y,
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
            self._gesture_client.post(
                f"{self._s}/actions",
                json={
                    "actions": [
                        {
                            "type": "pointer",
                            "id": "finger1",
                            "parameters": {"pointerType": "touch"},
                            "actions": [
                                {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                                {"type": "pointerDown", "button": 0},
                                {"type": "pause", "duration": duration},
                                {"type": "pointerUp", "button": 0},
                            ],
                        }
                    ],
                },
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout on tap(%d, %d) — gesture likely executed", x, y)

    def double_tap(self, x: int, y: int) -> None:
        try:
            self._gesture_client.post(
                f"{self._s}/actions",
                json={
                    "actions": [
                        {
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
                        }
                    ],
                },
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            logger.warning("Timeout on double_tap(%d, %d) — gesture likely executed", x, y)

    def swipe(
        self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.5
    ) -> None:
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
        for bundle in (
            "com.apple.AppStore",
            "com.apple.mobilesafari",
            "com.zhiliaoapp.musically",
            "com.burbn.instagram",
        ):
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

    def _stabilize_home_for_system_gesture(self, attempts: int = 3) -> None:
        """Return to the home screen before sending Control Center gestures with retry."""
        for attempt in range(attempts):
            try:
                self.press_button("home")
                time.sleep(0.5)
                self.press_button("home")
                time.sleep(1.0 if attempt == 0 else 0.5)
                return
            except Exception as e:
                logger.debug("Home button press failed (attempt %d): %s", attempt + 1, e)
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                continue
        logger.warning(
            "Could not stabilize home screen after %d attempts on %s", attempts, self.device.name
        )

    def _control_center_is_visible(self) -> bool:
        """Best-effort check that Control Center is actually open."""
        return bool(
            self._find_control_center_toggle("airplane")
            or self._find_control_center_toggle("wifi")
            or self._find_control_center_toggle("cellular")
        )

    def _open_control_center(self, max_attempts: int = None) -> bool:
        """Open Control Center from the top-right corner with retry logic and exponential backoff."""
        from sovi.config import settings

        # Use settings if available, otherwise use defaults
        if max_attempts is None:
            max_attempts = getattr(settings, "control_center_max_attempts", 5)
        base_delay = getattr(settings, "control_center_base_delay_seconds", 1.5)
        backoff_mult = getattr(settings, "control_center_backoff_multiplier", 1.5)

        size = self.screen_size()
        w, h = size["width"], size["height"]

        # Improved gesture patterns with better success rates
        gestures = [
            # (start_x_pct, start_y_pct, end_x_pct, end_y_pct, duration, label)
            (0.95, 0.02, 0.70, 0.40, 0.20, "standard_swipe"),
            (0.92, 0.04, 0.65, 0.50, 0.25, "deeper_swipe"),
            (0.97, 0.01, 0.60, 0.60, 0.30, "aggressive_swipe"),
            (0.90, 0.05, 0.50, 0.45, 0.35, "gentle_swipe"),
            (0.94, 0.03, 0.75, 0.35, 0.22, "quick_swipe"),
        ]

        for attempt in range(max_attempts):
            # Calculate exponential backoff delay
            delay = base_delay * (backoff_mult**attempt)

            # Select gesture (cycle through or random for variety)
            gesture_idx = attempt % len(gestures)
            start_x, start_y, end_x, end_y, duration, label = gestures[gesture_idx]

            logger.debug(
                "Control Center attempt %d/%d on %s using %s (delay=%.1fs)",
                attempt + 1,
                max_attempts,
                self.device.name,
                label,
                delay,
            )

            self._stabilize_home_for_system_gesture(attempts=2)

            try:
                self.swipe(
                    int(w * start_x),
                    max(1, int(h * start_y)),
                    int(w * end_x),
                    int(h * end_y),
                    duration=duration,
                )
                time.sleep(delay)

                if self._control_center_is_visible():
                    logger.info(
                        "Control Center opened successfully on %s (attempt %d, %s)",
                        self.device.name,
                        attempt + 1,
                        label,
                    )
                    return True

            except RuntimeError as e:
                logger.warning(
                    "WDA runtime error opening Control Center (attempt %d): %s", attempt + 1, e
                )
                if self.reconnect(attempts=1, delay_s=0.5):
                    continue
                if attempt < max_attempts - 1:
                    time.sleep(2.0 * (attempt + 1))  # Exponential backoff on WDA errors
                continue
            except Exception as e:
                logger.warning("Error opening Control Center (attempt %d): %s", attempt + 1, e)
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
                continue

        logger.warning(
            "Could not open Control Center on %s after %d attempts", self.device.name, max_attempts
        )
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
        elif toggle == "cellular":
            cellular_switch = (
                'type == "XCUIElementTypeSwitch" AND '
                '(name CONTAINS[c] "Cellular" OR label CONTAINS[c] "Cellular" '
                'OR name CONTAINS[c] "Mobile Data" OR label CONTAINS[c] "Mobile Data")'
            )
            cellular_button = (
                'type == "XCUIElementTypeButton" AND '
                '(name CONTAINS[c] "Cellular" OR label CONTAINS[c] "Cellular" '
                'OR name CONTAINS[c] "Mobile Data" OR label CONTAINS[c] "Mobile Data")'
            )
            candidates = [
                ("accessibility id", "cellular-data-button"),
                ("accessibility id", "mobile-data-button"),
                ("accessibility id", "cellular-button"),
                ("predicate string", cellular_switch),
                ("predicate string", cellular_button),
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
                ("airplane" in value and "on" in value) or value == "on" for value in normalized
            ):
                return True
            if any(
                ("airplane" in value and "off" in value) or value in {"airplane", "airplane mode"}
                for value in normalized
            ):
                return False
        elif toggle == "cellular":
            if any("off" in value or "disabled" in value for value in normalized):
                return False
            if any(
                (("cellular" in value or "mobile" in value) and "on" in value)
                or (("cellular" in value or "mobile" in value) and "," in value)
                or value in {"cellular on", "mobile data on"}
                for value in normalized
            ):
                return True

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

    def _set_control_center_toggle(
        self, toggle: str, *, desired_on: bool, attempts: int = 3
    ) -> bool:
        """Set a Control Center toggle to a desired state and verify it."""
        if toggle == "airplane" and desired_on:
            logger.critical("Blocked request to enable airplane mode on %s", self.device.name)
            return False

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
        """Ensure airplane mode is off with retry logic."""
        max_attempts = 3
        for attempt in range(max_attempts):
            opened = False
            try:
                opened = self._open_control_center()
                if not opened:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            "Could not open Control Center for airplane check (attempt %d/%d), retrying...",
                            attempt + 1,
                            max_attempts,
                        )
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    return False
                ok = self._set_control_center_toggle("airplane", desired_on=False)
                if ok:
                    logger.info("Airplane mode confirmed off on %s", self.device.name)
                    return True
                else:
                    logger.warning(
                        "Could not verify airplane mode off on %s (attempt %d/%d)",
                        self.device.name,
                        attempt + 1,
                        max_attempts,
                    )
                    if attempt < max_attempts - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    return False
            except Exception as e:
                logger.error(
                    "Failed to ensure airplane mode off on %s (attempt %d/%d): %s",
                    self.device.name,
                    attempt + 1,
                    max_attempts,
                    e,
                )
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
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
        return False

    def cellular_data_enabled(self) -> bool | None:
        """Return the current Control Center cellular-data state when it can be inferred."""
        opened = False
        try:
            opened = self._open_control_center()
            if not opened:
                return None
            _, state = self._read_control_center_toggle_state("cellular")
            return state
        except Exception:
            logger.error(
                "Failed to read cellular-data state on %s", self.device.name, exc_info=True
            )
            return None
        finally:
            try:
                if opened:
                    self._close_control_center()
            except Exception:
                try:
                    self.press_button("home")
                except Exception:
                    pass

    def set_cellular_data_enabled(self, enabled: bool, max_attempts: int = 3) -> bool:
        """Set the Control Center cellular-data toggle to the requested state with retry logic."""
        for attempt in range(max_attempts):
            opened = False
            try:
                opened = self._open_control_center()
                if not opened:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            "Could not open Control Center for cellular toggle (attempt %d/%d), retrying...",
                            attempt + 1,
                            max_attempts,
                        )
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    return False
                ok = self._set_control_center_toggle("cellular", desired_on=enabled)
                if ok:
                    logger.info(
                        "Cellular data confirmed %s on %s",
                        "on" if enabled else "off",
                        self.device.name,
                    )
                    return True
                else:
                    logger.warning(
                        "Could not verify cellular data %s on %s (attempt %d/%d)",
                        "on" if enabled else "off",
                        self.device.name,
                        attempt + 1,
                        max_attempts,
                    )
                    if attempt < max_attempts - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    return False
            except Exception as e:
                logger.error(
                    "Failed to set cellular data %s on %s (attempt %d/%d): %s",
                    "on" if enabled else "off",
                    self.device.name,
                    attempt + 1,
                    max_attempts,
                    e,
                )
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
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

    def ensure_cellular_data_on(self) -> bool:
        """Ensure the device's cellular/mobile-data toggle is enabled."""
        return self.set_cellular_data_enabled(True)

    @classmethod
    def _carrier_probe_succeeded(cls, source: Any) -> bool:
        """Recognize the captive-portal success page returned by a working carrier path.

        Updated to be more flexible with various carrier responses and iOS versions.
        """
        text = str(source).strip().lower()

        # Check for failure markers first (explicit failures)
        failure_markers = cls._CARRIER_PROBE_FAILURE_MARKERS + (
            "no connection",
            "no internet",
            "please connect",
            "login required",
            "authentication",
            "terms of service",
        )
        if any(marker in text for marker in failure_markers):
            return False

        # Check for success indicators - more comprehensive list
        success_markers = cls._CARRIER_PROBE_SUCCESS_MARKERS + (
            "success",
            "<body>",
            "<html>",
            "hotspot",
            "detect",
            "apple.com",
            "captive",
            "200",
            "OK",
            "<!doctype",
            "<?xml",
            "connected",
            "online",
        )

        # If we got any content at all and no failure markers, likely successful
        if any(marker in text for marker in success_markers):
            return True

        # If page has substantial content (not empty/error), consider it success
        if len(text) > 50 and not any(fail in text for fail in failure_markers):
            logger.debug(
                "Carrier probe: No explicit success markers but substantial content found (%d chars)",
                len(text),
            )
            return True

        return False

    def probe_cellular_connectivity(
        self,
        *,
        attempts: int = None,
        wait_s: float = None,
        url: str | None = None,
        cleanup: bool = True,
    ) -> bool:
        """Actively prove the device can reach the public internet over the carrier path.

        Uses exponential backoff between attempts and tries multiple probe URLs for reliability.
        """
        from sovi.config import settings

        # Use settings defaults if not provided
        if attempts is None:
            attempts = getattr(settings, "device_network_probe_attempts", 3)
        if wait_s is None:
            wait_s = getattr(settings, "device_network_probe_wait_seconds", 4.0)

        # Try primary URL first, then backup URLs
        urls_to_try = (
            [url] if url else [self._CARRIER_PROBE_URL] + list(self._CARRIER_PROBE_BACKUP_URLS)
        )
        base_wait = wait_s

        for url_idx, probe_url in enumerate(urls_to_try):
            logger.debug("Trying probe URL %d/%d: %s", url_idx + 1, len(urls_to_try), probe_url)

            for attempt in range(max(attempts, 1)):
                # Exponential backoff for wait time
                current_wait = base_wait * (1.2**attempt)

                request_url = probe_url
                if url is None:
                    request_url = f"{probe_url}?_={time.time_ns()}"

                logger.debug(
                    "Carrier probe attempt %d/%d on %s using %s",
                    attempt + 1,
                    attempts,
                    self.device.name,
                    probe_url,
                )

                try:
                    self.open_url(request_url)
                    time.sleep(current_wait)

                    page_source = self.source()
                    if self._carrier_probe_succeeded(page_source):
                        logger.info(
                            "Carrier reachability probe succeeded on %s (attempt %d/%d, URL: %s)",
                            self.device.name,
                            attempt + 1,
                            attempts,
                            probe_url,
                        )
                        return True
                    else:
                        logger.debug(
                            "Carrier probe attempt %d/%d did not detect success on %s (URL: %s)",
                            attempt + 1,
                            attempts,
                            self.device.name,
                            probe_url,
                        )

                except RuntimeError as e:
                    logger.warning(
                        "WDA runtime error during carrier probe (attempt %d): %s", attempt + 1, e
                    )
                    if not self.reconnect(attempts=1, delay_s=0.5):
                        logger.warning(
                            "WDA reconnect failed during carrier probe on %s", self.device.name
                        )
                        if attempt < attempts - 1:
                            time.sleep(1.0 * (attempt + 1))
                        continue

                except Exception:
                    logger.debug(
                        "Carrier reachability probe source read failed on %s (attempt %d/%d)",
                        self.device.name,
                        attempt + 1,
                        attempts,
                        exc_info=True,
                    )
                    if attempt < attempts - 1:
                        time.sleep(0.5 * (attempt + 1))

        logger.warning(
            "Carrier reachability probe failed on %s after trying %d URLs with %d attempts each",
            self.device.name,
            len(urls_to_try),
            attempts,
        )
        return False

    def probe_carrier_reachability(
        self,
        *,
        attempts: int = 4,
        wait_s: float = 5.0,
        url: str | None = None,
        cleanup: bool = True,
    ) -> bool:
        """Backward-compatible alias for caller paths that still use the older probe name."""
        return self.probe_cellular_connectivity(
            attempts=attempts,
            wait_s=wait_s,
            url=url,
            cleanup=cleanup,
        )

    def reset_cellular_data_connection(
        self,
        *,
        wait_off_seconds: float = None,
        recovery_wait_s: float = None,
        probe_attempts: int = None,
        probe_wait_s: float = None,
    ) -> bool:
        """Cycle cellular data OFF, restore it, and prove carrier recovery.

        Uses exponential backoff and improved retry logic for reliability.
        """
        from sovi.config import settings

        # Use settings defaults if not provided
        if wait_off_seconds is None:
            wait_off_seconds = getattr(settings, "device_network_reset_disabled_seconds", 45)
        if recovery_wait_s is None:
            recovery_wait_s = getattr(settings, "device_network_reset_settle_seconds", 8)
        if probe_attempts is None:
            probe_attempts = getattr(settings, "device_network_probe_attempts", 3)
        if probe_wait_s is None:
            probe_wait_s = getattr(settings, "device_network_probe_wait_seconds", 4.0)

        try:
            logger.info("Starting cellular data reset on %s", self.device.name)

            # Step 1: Ensure airplane mode is off
            if not self.ensure_airplane_mode_off():
                logger.error("Could not ensure airplane mode off on %s", self.device.name)
                return False

            # Step 2: Ensure WiFi is off
            if not self.ensure_wifi_off():
                logger.error("Could not ensure WiFi off on %s", self.device.name)
                return False

            # Step 3: Disable cellular data
            logger.info(
                "Disabling cellular data on %s for %.0f seconds", self.device.name, wait_off_seconds
            )
            if not self.set_cellular_data_enabled(False):
                logger.error("Could not disable cellular data on %s", self.device.name)
                return False

            time.sleep(wait_off_seconds)

            # Step 4: Re-enable cellular data
            logger.info("Re-enabling cellular data on %s", self.device.name)
            if not self.set_cellular_data_enabled(True):
                logger.error("Could not re-enable cellular data on %s", self.device.name)
                return False

            time.sleep(recovery_wait_s)

            # Step 5: Reconnect WDA session
            if not self.reconnect():
                logger.warning(
                    "Could not reconnect WDA session on %s after cellular reset", self.device.name
                )
                # Try once more with longer delay
                time.sleep(3.0)
                if not self.reconnect():
                    return False

            # Step 6: Ensure cellular-only state
            if not self.ensure_cellular_only():
                logger.warning(
                    "Cellular reset on %s did not restore cellular-only state", self.device.name
                )
                return False

            # Step 7: Verify connectivity
            logger.info("Verifying carrier connectivity on %s after reset", self.device.name)
            return self.probe_cellular_connectivity(
                attempts=probe_attempts,
                wait_s=probe_wait_s,
                cleanup=False,
            )
        except Exception:
            logger.error("Failed to reset cellular data on %s", self.device.name, exc_info=True)
            return False
        finally:
            self.reset_to_home()

    def reset_cellular_data(
        self,
        *,
        disabled_wait_s: float = 60.0,
        recovery_wait_s: float = 10.0,
        probe_attempts: int = 4,
        probe_wait_s: float = 5.0,
    ) -> bool:
        """Backward-compatible alias for callers migrating off airplane-mode rotation."""
        return self.reset_cellular_data_connection(
            wait_off_seconds=disabled_wait_s,
            recovery_wait_s=recovery_wait_s,
            probe_attempts=probe_attempts,
            probe_wait_s=probe_wait_s,
        )

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
        """Force the device into the expected network state: airplane off, cellular on, Wi-Fi off."""
        if not self.ensure_airplane_mode_off():
            return False
        if not self.ensure_cellular_data_on():
            return False
        return self.ensure_wifi_off()

    def ensure_cellular_ready(
        self,
        *,
        probe_attempts: int = 4,
        probe_wait_s: float = 5.0,
        cleanup: bool = False,
    ) -> bool:
        """Enforce cellular-only radio state and prove that carrier data is reachable."""
        if not self.ensure_cellular_only():
            return False
        return self.probe_cellular_connectivity(
            attempts=probe_attempts,
            wait_s=probe_wait_s,
            cleanup=cleanup,
        )

    def toggle_airplane_mode(self, wait_after: float = 6.0) -> bool:
        """Toggle airplane mode for IP rotation (legacy method).

        Note: This is kept for backward compatibility but the cellular reset flow is preferred.
        """
        try:
            if not self.ensure_airplane_mode_off():
                return False

            # Open Control Center
            if not self._open_control_center():
                logger.error(
                    "Could not open Control Center for airplane mode toggle on %s", self.device.name
                )
                return False

            # Enable airplane mode
            if not self._set_control_center_toggle("airplane", desired_on=True):
                logger.error("Could not enable airplane mode on %s", self.device.name)
                return False

            time.sleep(wait_after)

            # Disable airplane mode
            if not self._set_control_center_toggle("airplane", desired_on=False):
                logger.error("Could not disable airplane mode on %s", self.device.name)
                return False

            time.sleep(2.0)
            return True
        except Exception as e:
            logger.error("Failed to toggle airplane mode on %s: %s", self.device.name, e)
            return False
        finally:
            try:
                self._close_control_center()
            except Exception:
                pass


# --- App bundle identifiers ---

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
