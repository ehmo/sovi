"""Appium client wrapper for iOS device automation."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

# Bundle IDs
BUNDLE_IDS = {
    "tiktok": "com.zhiliaoapp.musically",
    "instagram": "com.burbn.instagram",
    "youtube": "com.google.ios.youtube",
    "youtube_shorts": "com.google.ios.youtube",  # DB platform_type alias
    "reddit": "com.reddit.Reddit",
    "twitter": "com.atebits.Tweetie2",
    "x_twitter": "com.atebits.Tweetie2",  # DB platform_type alias
    "settings": "com.apple.Preferences",
    "safari": "com.apple.mobilesafari",
}


@dataclass
class DeviceConfig:
    """Configuration for a connected iPhone."""

    udid: str
    wda_port: int
    name: str = ""
    appium_port: int = 4723


@dataclass
class DeviceSession:
    """Active Appium session on a device."""

    device: DeviceConfig
    driver: webdriver.Remote | None = None
    _active_app: str = ""

    def is_alive(self) -> bool:
        if not self.driver:
            return False
        try:
            self.driver.session_id  # noqa: B018
            return True
        except WebDriverException:
            return False


class AppiumClient:
    """Manages Appium sessions across multiple devices."""

    def __init__(self, devices: list[DeviceConfig]) -> None:
        self.devices = {d.udid: d for d in devices}
        self.sessions: dict[str, DeviceSession] = {}

    def connect(self, udid: str) -> DeviceSession:
        """Create or reuse an Appium session for a device."""
        if udid in self.sessions and self.sessions[udid].is_alive():
            return self.sessions[udid]

        device = self.devices[udid]
        opts = XCUITestOptions()
        opts.udid = udid
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        opts.no_reset = True
        opts.set_capability("usePreinstalledWDA", True)
        opts.set_capability("updatedWDABundleId", "com.ehmo.WebDriverAgentRunner")
        opts.set_capability("webDriverAgentUrl", f"http://localhost:{device.wda_port}")
        opts.set_capability("autoAcceptAlerts", False)
        opts.set_capability("autoDismissAlerts", False)
        opts.set_capability("newCommandTimeout", 300)

        url = f"http://127.0.0.1:{device.appium_port}"
        logger.info("Connecting to device %s via %s (WDA port %d)", udid[:8], url, device.wda_port)
        driver = webdriver.Remote(url, options=opts)
        session = DeviceSession(device=device, driver=driver)
        self.sessions[udid] = session
        return session

    def disconnect(self, udid: str) -> None:
        session = self.sessions.pop(udid, None)
        if session and session.driver:
            try:
                session.driver.quit()
            except WebDriverException:
                pass

    def disconnect_all(self) -> None:
        for udid in list(self.sessions):
            self.disconnect(udid)


class DeviceAutomator:
    """High-level automation actions on a single device session."""

    def __init__(self, session: DeviceSession) -> None:
        self.session = session
        self.driver = session.driver

    # --- Timing helpers (human-like) ---

    def _human_delay(self, min_s: float = 0.3, max_s: float = 1.5) -> None:
        time.sleep(random.uniform(min_s, max_s))

    def _read_delay(self, text_len: int = 50) -> None:
        """Simulate reading time based on text length."""
        wpm = random.uniform(200, 300)
        words = text_len / 5
        time.sleep(max(1.0, (words / wpm) * 60))

    def _scroll_delay(self) -> None:
        time.sleep(random.uniform(0.5, 2.0))

    # --- App management ---

    def launch_app(self, app_name: str) -> None:
        bundle_id = BUNDLE_IDS.get(app_name, app_name)
        logger.info("Launching %s (%s)", app_name, bundle_id)
        self.driver.activate_app(bundle_id)
        self.session._active_app = bundle_id
        time.sleep(random.uniform(2.0, 4.0))

    def terminate_app(self, app_name: str) -> None:
        bundle_id = BUNDLE_IDS.get(app_name, app_name)
        self.driver.terminate_app(bundle_id)
        self.session._active_app = ""

    def is_app_running(self, app_name: str) -> bool:
        bundle_id = BUNDLE_IDS.get(app_name, app_name)
        state = self.driver.query_app_state(bundle_id)
        return state >= 4  # RUNNING_IN_FOREGROUND

    # --- Element interaction ---

    def find_element(
        self,
        by: str = AppiumBy.ACCESSIBILITY_ID,
        value: str = "",
        timeout: float = 10.0,
    ):
        """Find element with explicit wait."""
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.presence_of_element_located((by, value)))

    def find_elements(self, by: str = AppiumBy.ACCESSIBILITY_ID, value: str = ""):
        return self.driver.find_elements(by, value)

    def tap(self, by: str, value: str, timeout: float = 10.0) -> None:
        self._human_delay(0.2, 0.5)
        el = self.find_element(by, value, timeout)
        el.click()

    def type_text(self, by: str, value: str, text: str, timeout: float = 10.0) -> None:
        el = self.find_element(by, value, timeout)
        el.click()
        self._human_delay(0.3, 0.7)
        # Type character by character for human-like behavior
        for char in text:
            el.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))

    def swipe_up(self, duration_ms: int = 800) -> None:
        """Swipe up (scroll down) on screen."""
        size = self.driver.get_window_size()
        start_x = size["width"] // 2
        start_y = int(size["height"] * 0.75)
        end_y = int(size["height"] * 0.25)
        self.driver.swipe(start_x, start_y, start_x, end_y, duration_ms)

    def swipe_down(self, duration_ms: int = 800) -> None:
        """Swipe down (scroll up) on screen."""
        size = self.driver.get_window_size()
        start_x = size["width"] // 2
        start_y = int(size["height"] * 0.25)
        end_y = int(size["height"] * 0.75)
        self.driver.swipe(start_x, start_y, start_x, end_y, duration_ms)

    def tap_coordinates(self, x: int, y: int) -> None:
        """Tap at absolute coordinates."""
        self._human_delay(0.2, 0.4)
        self.driver.tap([(x, y)])

    # --- Dialog / popup handling ---

    def dismiss_system_alerts(self) -> bool:
        """Dismiss iOS system alerts (notifications, tracking, etc.)."""
        dismissed = False
        alert_buttons = [
            "Allow",
            "Don\u2019t Allow",
            "Not Now",
            "OK",
            "Later",
            "Skip",
            "Continue",
        ]
        for btn_text in alert_buttons:
            try:
                el = self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, btn_text)
                el.click()
                dismissed = True
                logger.info("Dismissed system alert: %s", btn_text)
                time.sleep(0.5)
                break
            except NoSuchElementException:
                continue
        return dismissed

    def dismiss_app_popups(self) -> bool:
        """Try to dismiss in-app popups and modals."""
        dismissed = False
        dismiss_ids = [
            "Not Now",
            "Skip",
            "Later",
            "Got it",
            "Dismiss",
            "Close",
            "No thanks",
            "Maybe later",
            "Not interested",
        ]
        for label in dismiss_ids:
            try:
                el = self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, label)
                el.click()
                dismissed = True
                logger.info("Dismissed popup: %s", label)
                time.sleep(0.5)
            except NoSuchElementException:
                continue

        # Try X buttons
        try:
            close_buttons = self.driver.find_elements(
                AppiumBy.IOS_PREDICATE,
                'name CONTAINS "close" OR name CONTAINS "dismiss" OR label == "X"',
            )
            if close_buttons:
                close_buttons[0].click()
                dismissed = True
        except (NoSuchElementException, StaleElementReferenceException):
            pass

        return dismissed

    # --- Screenshot ---

    def screenshot(self, path: str | None = None) -> bytes:
        """Take screenshot, optionally save to file."""
        png = self.driver.get_screenshot_as_png()
        if path:
            with open(path, "wb") as f:
                f.write(png)
        return png
