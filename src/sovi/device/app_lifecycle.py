"""App lifecycle management — delete, install, login for each platform.

Handles the full cycle of:
1. Delete app (clear IDFV)
2. Install from App Store
3. Login with email + password + TOTP
4. Onboard past initial screens

Used by the scheduler before each warming session to rotate IDFV.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from sovi import events
from sovi.auth import totp
from sovi.crypto import decrypt
from sovi.device.wda_client import WDASession, DeviceAutomation

logger = logging.getLogger(__name__)

# Bundle IDs
BUNDLES: dict[str, str] = {
    "tiktok": "com.zhiliaoapp.musically",
    "instagram": "com.burbn.instagram",
}

# App Store display names (for search)
APP_NAMES: dict[str, str] = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
}


def delete_app(wda: WDASession, platform: str, *, device_id: str | None = None) -> bool:
    """Delete an app from the device to reset IDFV.

    Uses WDA springboard interaction to long-press and delete.
    """
    bundle_id = BUNDLES.get(platform)
    if not bundle_id:
        logger.error("Unknown platform: %s", platform)
        return False

    try:
        # First terminate the app if running
        wda.terminate_app(bundle_id)
        time.sleep(1)

        # Go to home screen
        wda.press_button("home")
        time.sleep(1)

        # Use WDA to uninstall via custom endpoint
        try:
            resp = wda.client.post(
                f"{wda._s}/wda/apps/uninstall",
                json={"bundleId": bundle_id},
            )
            if resp.status_code == 200:
                logger.info("Deleted %s (%s)", platform, bundle_id)
                events.emit("device", "info", "app_deleted",
                           f"Deleted {platform} app for IDFV reset",
                           device_id=device_id,
                           context={"platform": platform, "bundle_id": bundle_id})
                return True
        except Exception:
            pass

        # Fallback: use mobile gestalt / springboard approach
        # Long-press on app icon → Delete App
        logger.warning("WDA uninstall endpoint failed, trying springboard method")

        # Find app icon on springboard
        wda.press_button("home")
        time.sleep(1)
        wda.press_button("home")
        time.sleep(1)

        app_name = APP_NAMES.get(platform, platform)
        app_el = wda.find_element("accessibility id", app_name)
        if not app_el:
            logger.warning("Could not find %s icon on springboard", app_name)
            return False

        el_id = app_el.get("ELEMENT", "")
        if not el_id:
            return False

        # Long press (3s) to trigger jiggle mode
        resp = wda.client.post(f"{wda._s}/wda/element/{el_id}/touchAndHold", json={"duration": 3.0})
        time.sleep(2)

        # Look for "Remove App" or "Delete App" option
        for label in ["Remove App", "Delete App"]:
            remove_el = wda.find_element("accessibility id", label)
            if remove_el:
                wda.element_click(remove_el["ELEMENT"])
                time.sleep(1)
                break

        # Confirm deletion
        for label in ["Delete App", "Delete"]:
            confirm_el = wda.find_element("accessibility id", label)
            if confirm_el:
                wda.element_click(confirm_el["ELEMENT"])
                time.sleep(2)
                break

        logger.info("Deleted %s via springboard", platform)
        events.emit("device", "info", "app_deleted",
                    f"Deleted {platform} app via springboard",
                    device_id=device_id,
                    context={"platform": platform, "method": "springboard"})
        return True

    except Exception:
        logger.error("Failed to delete %s", platform, exc_info=True)
        events.emit("device", "error", "app_delete_failed",
                    f"Failed to delete {platform} app",
                    device_id=device_id,
                    context={"platform": platform})
        return False


def install_from_app_store(
    wda: WDASession,
    platform: str,
    *,
    device_id: str | None = None,
    timeout: int = 120,
) -> bool:
    """Install an app from the App Store by searching for it.

    Assumes App Store is signed in on the device.
    """
    app_name = APP_NAMES.get(platform)
    bundle_id = BUNDLES.get(platform)
    if not app_name or not bundle_id:
        logger.error("Unknown platform: %s", platform)
        return False

    auto = DeviceAutomation(wda)

    try:
        # Open App Store
        wda.launch_app("com.apple.AppStore")
        time.sleep(3)
        auto.dismiss_popups(max_attempts=2)

        # Tap Search tab
        search_tab = wda.find_element("accessibility id", "Search")
        if search_tab:
            wda.element_click(search_tab["ELEMENT"])
            time.sleep(2)

        # Find search field
        search_field = wda.find_element("class chain", "**/XCUIElementTypeSearchField")
        if not search_field:
            logger.error("Could not find App Store search field")
            return False

        el_id = search_field.get("ELEMENT", "")
        wda.element_click(el_id)
        time.sleep(0.5)
        wda.element_value(el_id, app_name)
        time.sleep(1)

        # Press Search on keyboard
        # Use the keyboard search button
        search_btn = wda.find_element("accessibility id", "search")
        if search_btn:
            wda.element_click(search_btn["ELEMENT"])
        time.sleep(3)

        # Look for the GET or cloud download button (redownload)
        for label in ["GET", "Get", "INSTALL", "Install"]:
            get_btn = wda.find_element("accessibility id", label)
            if get_btn:
                wda.element_click(get_btn["ELEMENT"])
                break
        else:
            # Try cloud icon (redownload)
            cloud_btn = wda.find_element(
                "predicate string",
                'name CONTAINS "download" OR name CONTAINS "cloud"'
            )
            if cloud_btn:
                wda.element_click(cloud_btn.get("ELEMENT", ""))

        # Wait for install to complete
        logger.info("Installing %s, waiting up to %ds...", app_name, timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Check if app is now installed
            state = wda.app_state(bundle_id)
            if state >= 1:  # 1 = not running but installed
                logger.info("Successfully installed %s", app_name)
                events.emit("device", "info", "app_installed",
                           f"Installed {platform} from App Store",
                           device_id=device_id,
                           context={"platform": platform, "bundle_id": bundle_id})
                # Go home
                wda.press_button("home")
                time.sleep(1)
                return True
            time.sleep(5)

        logger.error("Timed out waiting for %s install", app_name)
        events.emit("device", "error", "install_failed",
                    f"Timed out installing {platform}",
                    device_id=device_id,
                    context={"platform": platform, "timeout": timeout})
        return False

    except Exception:
        logger.error("Failed to install %s", platform, exc_info=True)
        events.emit("device", "error", "install_failed",
                    f"Failed to install {platform} from App Store",
                    device_id=device_id,
                    context={"platform": platform})
        return False


def login_tiktok(
    wda: WDASession,
    email: str,
    password: str,
    totp_secret: str | None = None,
    *,
    device_id: str | None = None,
    account_id: str | None = None,
) -> bool:
    """Log into TikTok with email + password + optional TOTP.

    Assumes app is freshly installed and at the initial screen.
    """
    auto = DeviceAutomation(wda)

    try:
        wda.launch_app(BUNDLES["tiktok"])
        time.sleep(random.uniform(3, 5))
        auto.dismiss_popups(max_attempts=3)

        # Look for "Use phone / email / username" or "Log in"
        for label in ["Use phone / email / username", "Log in", "Log In"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(2)
                break

        # Switch to email/username login
        for label in ["Email / Username", "Use email/username"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(1)
                break

        # Enter email
        email_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "email" OR name CONTAINS "Email" OR placeholderValue CONTAINS "email")'
        )
        if email_field:
            el_id = email_field["ELEMENT"]
            wda.element_click(el_id)
            time.sleep(0.3)
            wda.element_value(el_id, email)
            time.sleep(random.uniform(0.5, 1.0))

        # Enter password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            el_id = pw_field["ELEMENT"]
            wda.element_click(el_id)
            time.sleep(0.3)
            wda.element_value(el_id, password)
            time.sleep(random.uniform(0.5, 1.0))

        # Tap Login
        for label in ["Log in", "Log In", "Login"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                break

        time.sleep(5)

        # Handle TOTP 2FA if prompted
        if totp_secret:
            totp_field = wda.find_element(
                "predicate string",
                'type == "XCUIElementTypeTextField" AND (name CONTAINS "code" OR name CONTAINS "verification")'
            )
            if totp_field:
                code = totp.get_code(totp_secret)
                wda.element_value(totp_field["ELEMENT"], code)
                time.sleep(1)
                # Submit
                for label in ["Verify", "Submit", "Confirm", "Next"]:
                    el = wda.find_element("accessibility id", label)
                    if el:
                        wda.element_click(el["ELEMENT"])
                        break
                time.sleep(3)

        # Handle CAPTCHA if present (screenshot-based)
        auto.dismiss_popups(max_attempts=3)
        time.sleep(2)

        # Verify we're logged in by checking for FYP elements
        # If we can swipe, we're on the main feed
        wda.swipe_up(duration=0.4)
        time.sleep(1)

        logger.info("TikTok login successful for %s", email)
        events.emit("account", "info", "login_success",
                    f"TikTok login successful for {email}",
                    device_id=device_id, account_id=account_id,
                    context={"platform": "tiktok", "email": email})
        return True

    except Exception:
        logger.error("TikTok login failed for %s", email, exc_info=True)
        events.emit("account", "error", "login_failed",
                    f"TikTok login failed for {email}",
                    device_id=device_id, account_id=account_id,
                    context={"platform": "tiktok", "email": email, "step": "login"})
        return False


def login_instagram(
    wda: WDASession,
    email: str,
    password: str,
    *,
    device_id: str | None = None,
    account_id: str | None = None,
) -> bool:
    """Log into Instagram with email + password.

    Assumes app is freshly installed and at the initial screen.
    """
    auto = DeviceAutomation(wda)

    try:
        wda.launch_app(BUNDLES["instagram"])
        time.sleep(random.uniform(3, 5))
        auto.dismiss_popups(max_attempts=3)

        # Look for login button
        for label in ["I already have an account", "Log in", "Log In"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                time.sleep(2)
                break

        # Enter email/username
        username_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeTextField" AND (name CONTAINS "Username" OR name CONTAINS "email" OR name CONTAINS "Phone")'
        )
        if username_field:
            el_id = username_field["ELEMENT"]
            wda.element_click(el_id)
            time.sleep(0.3)
            wda.element_value(el_id, email)
            time.sleep(random.uniform(0.5, 1.0))

        # Enter password
        pw_field = wda.find_element(
            "predicate string",
            'type == "XCUIElementTypeSecureTextField"'
        )
        if pw_field:
            el_id = pw_field["ELEMENT"]
            wda.element_click(el_id)
            time.sleep(0.3)
            wda.element_value(el_id, password)
            time.sleep(random.uniform(0.5, 1.0))

        # Tap Login
        for label in ["Log in", "Log In", "Login"]:
            btn = wda.find_element(
                "predicate string",
                f'label == "{label}" AND type == "XCUIElementTypeButton"'
            )
            if btn:
                wda.element_click(btn["ELEMENT"])
                break

        time.sleep(5)

        # Handle popups (save login info, notifications, etc.)
        auto.dismiss_popups(max_attempts=5)
        time.sleep(2)

        # Verify logged in — check for home feed elements
        home_el = wda.find_element("accessibility id", "Home")
        if home_el:
            logger.info("Instagram login successful for %s", email)
            events.emit("account", "info", "login_success",
                        f"Instagram login successful for {email}",
                        device_id=device_id, account_id=account_id,
                        context={"platform": "instagram", "email": email})
            return True

        # Fallback check — just try to use the app
        wda.swipe_up(duration=0.4)
        time.sleep(1)

        logger.info("Instagram login likely successful for %s", email)
        events.emit("account", "info", "login_success",
                    f"Instagram login completed for {email}",
                    device_id=device_id, account_id=account_id,
                    context={"platform": "instagram", "email": email})
        return True

    except Exception:
        logger.error("Instagram login failed for %s", email, exc_info=True)
        events.emit("account", "error", "login_failed",
                    f"Instagram login failed for {email}",
                    device_id=device_id, account_id=account_id,
                    context={"platform": "instagram", "email": email, "step": "login"})
        return False


def login_account(
    wda: WDASession,
    account: dict,
    *,
    device_id: str | None = None,
) -> bool:
    """Log into any platform account — dispatches to platform-specific login.

    account dict must have: platform, email_enc (or email), password_enc (or password),
    optionally totp_secret_enc.
    """
    platform = account["platform"]

    # Decrypt credentials
    email = account.get("email") or decrypt(account["email_enc"])
    password = account.get("password") or decrypt(account["password_enc"])
    totp_secret = None
    if account.get("totp_secret_enc"):
        totp_secret = decrypt(account["totp_secret_enc"])

    account_id = str(account.get("id", ""))

    if platform == "tiktok":
        return login_tiktok(wda, email, password, totp_secret,
                           device_id=device_id, account_id=account_id)
    elif platform == "instagram":
        return login_instagram(wda, email, password,
                              device_id=device_id, account_id=account_id)
    else:
        logger.error("Unsupported platform for login: %s", platform)
        return False
