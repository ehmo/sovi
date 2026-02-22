"""App onboarding automation — get past initial screens to usable state.

Handles: birthdate pickers, TOS dialogs, notification prompts, etc.
"""

from __future__ import annotations

import logging
import random
import time

from sovi.device.wda_client import WDASession, DeviceAutomation

logger = logging.getLogger(__name__)


def onboard_tiktok(wda: WDASession, auto: DeviceAutomation) -> bool:
    """Get TikTok past initial screens to the FYP.

    Returns True if FYP was reached successfully.
    """
    logger.info("Starting TikTok onboarding on %s", wda.device.name)
    auto.launch("tiktok")

    for attempt in range(8):
        time.sleep(2)

        # Check for birthdate picker
        pickers = wda.find_elements("class chain", "**/XCUIElementTypePickerWheel")
        if len(pickers) == 3:
            logger.info("Birthdate picker found, setting DOB...")
            wheel_ids = [p.get("ELEMENT", "") for p in pickers]
            # Random adult DOB
            month = random.choice([
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ])
            day = str(random.randint(1, 28))
            year = str(random.randint(1990, 2002))
            wda.client.post(
                f"/session/{wda.session_id}/element/{wheel_ids[0]}/value",
                json={"value": [month]},
            )
            time.sleep(0.3)
            wda.client.post(
                f"/session/{wda.session_id}/element/{wheel_ids[1]}/value",
                json={"value": [day]},
            )
            time.sleep(0.3)
            wda.client.post(
                f"/session/{wda.session_id}/element/{wheel_ids[2]}/value",
                json={"value": [year]},
            )
            time.sleep(0.5)

            next_el = wda.find_element("accessibility id", "Next")
            if next_el:
                wda.element_click(next_el["ELEMENT"])
                logger.info("Set DOB to %s %s %s, tapped Next", month, day, year)
                time.sleep(3)
            continue

        # Check for TOS dialog (by accessibility id)
        tos = wda.find_element("accessibility id", "Agree and continue")
        if tos:
            wda.element_click(tos["ELEMENT"])
            logger.info("Accepted TOS")
            time.sleep(3)
            continue

        # Check for notification/tracking prompts
        for label in ["Don't Allow", "Not Now", "Skip", "Later", "Close", "No thanks"]:
            el = wda.find_element("accessibility id", label)
            if el:
                wda.element_click(el["ELEMENT"])
                logger.info("Dismissed: %s", label)
                time.sleep(2)
                break

        # Check system alerts
        alert_text = wda.get_alert_text()
        if alert_text:
            wda.dismiss_alert()
            logger.info("Dismissed system alert")
            time.sleep(1)
            continue

        # Check if we reached the FYP — swipe_up works = we're on video feed
        # Quick heuristic: try a swipe, if it works without error we're likely on FYP
        try:
            wda.swipe_up(duration=0.4)
            time.sleep(1)
            # If we can swipe, we're past onboarding
            logger.info("TikTok onboarding complete — on FYP (attempt %d)", attempt)
            return True
        except Exception:
            pass

    logger.warning("TikTok onboarding may not have completed")
    return False


def onboard_instagram(wda: WDASession, auto: DeviceAutomation) -> bool:
    """Get Instagram past initial screens. Returns True if feed reached."""
    logger.info("Starting Instagram onboarding on %s", wda.device.name)
    auto.launch("instagram")

    # Instagram requires login — can't browse anonymously
    # Check if we see login/signup screen
    login_el = wda.find_element("accessibility id", "I already have an account")
    if login_el:
        logger.info("Instagram requires login — cannot browse anonymously")
        return False

    join_el = wda.find_element("accessibility id", "Join Instagram")
    if join_el:
        logger.info("Instagram requires account creation — cannot browse anonymously")
        return False

    # If we get here, we might be logged in
    logger.info("Instagram appears to be past onboarding")
    return True
