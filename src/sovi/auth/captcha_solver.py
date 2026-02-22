"""CAPTCHA solving via CapSolver API.

Handles slide CAPTCHAs and image recognition CAPTCHAs
that TikTok and Instagram present during signup/login.
"""

from __future__ import annotations

import base64
import logging
import time

import httpx

from sovi.config import settings
from sovi import events

logger = logging.getLogger(__name__)

CAPSOLVER_BASE = "https://api.capsolver.com"


def _create_task(task_type: str, task_params: dict) -> str | None:
    """Create a CapSolver task and return the task_id."""
    if not settings.capsolver_api_key:
        logger.error("CAPSOLVER_API_KEY not configured")
        return None

    try:
        resp = httpx.post(
            f"{CAPSOLVER_BASE}/createTask",
            json={
                "clientKey": settings.capsolver_api_key,
                "task": {"type": task_type, **task_params},
            },
            timeout=30.0,
        )
        data = resp.json()
        if data.get("errorId", 0) != 0:
            logger.error("CapSolver error: %s", data.get("errorDescription"))
            return None
        return data.get("taskId")
    except Exception:
        logger.error("Failed to create CapSolver task", exc_info=True)
        return None


def _get_result(task_id: str, timeout: int = 60, poll_interval: int = 3) -> dict | None:
    """Poll for task result."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = httpx.post(
                f"{CAPSOLVER_BASE}/getTaskResult",
                json={
                    "clientKey": settings.capsolver_api_key,
                    "taskId": task_id,
                },
                timeout=15.0,
            )
            data = resp.json()
            status = data.get("status")

            if status == "ready":
                return data.get("solution", {})
            elif status == "failed":
                logger.error("CapSolver task failed: %s", data.get("errorDescription"))
                return None

        except Exception:
            logger.warning("Error polling CapSolver result", exc_info=True)

        time.sleep(poll_interval)

    logger.warning("CapSolver task timed out (task_id=%s)", task_id)
    return None


def solve_slide(
    screenshot_png: bytes,
    *,
    platform: str = "unknown",
    device_id: str | None = None,
    account_id: str | None = None,
) -> dict | None:
    """Solve a slide/puzzle CAPTCHA from a screenshot.

    Returns dict with slide coordinates, or None on failure.
    """
    b64_image = base64.b64encode(screenshot_png).decode()

    task_id = _create_task("AntiSliderTaskByImage", {
        "image": b64_image,
    })
    if not task_id:
        events.emit("auth", "warning", "captcha_failed", f"Failed to create slide CAPTCHA task for {platform}",
                     device_id=device_id, account_id=account_id,
                     context={"platform": platform, "solver": "capsolver", "type": "slide"})
        return None

    result = _get_result(task_id)
    if result:
        logger.info("Slide CAPTCHA solved for %s", platform)
        return result

    events.emit("auth", "warning", "captcha_failed", f"Slide CAPTCHA solve timeout for {platform}",
                 device_id=device_id, account_id=account_id,
                 context={"platform": platform, "solver": "capsolver", "type": "slide", "task_id": task_id})
    return None


def solve_image(
    screenshot_png: bytes,
    question: str = "",
    *,
    platform: str = "unknown",
    device_id: str | None = None,
    account_id: str | None = None,
) -> dict | None:
    """Solve an image recognition CAPTCHA (e.g., 'select all buses').

    Returns dict with solution coordinates, or None on failure.
    """
    b64_image = base64.b64encode(screenshot_png).decode()

    task_id = _create_task("ImageToTextTask", {
        "body": b64_image,
        "question": question,
    })
    if not task_id:
        events.emit("auth", "warning", "captcha_failed", f"Failed to create image CAPTCHA task for {platform}",
                     device_id=device_id, account_id=account_id,
                     context={"platform": platform, "solver": "capsolver", "type": "image"})
        return None

    result = _get_result(task_id)
    if result:
        logger.info("Image CAPTCHA solved for %s", platform)
        return result

    events.emit("auth", "warning", "captcha_failed", f"Image CAPTCHA solve timeout for {platform}",
                 device_id=device_id, account_id=account_id,
                 context={"platform": platform, "solver": "capsolver", "type": "image", "task_id": task_id})
    return None


def solve_funcaptcha(
    public_key: str,
    page_url: str,
    *,
    platform: str = "unknown",
    device_id: str | None = None,
    account_id: str | None = None,
) -> str | None:
    """Solve a FunCaptcha (Arkose Labs) challenge — used by some TikTok flows.

    Returns the token string, or None on failure.
    """
    task_id = _create_task("FunCaptchaTaskProxyLess", {
        "websitePublicKey": public_key,
        "websiteURL": page_url,
    })
    if not task_id:
        return None

    result = _get_result(task_id, timeout=120)
    if result:
        return result.get("token")

    events.emit("auth", "warning", "captcha_failed", f"FunCaptcha solve timeout for {platform}",
                 device_id=device_id, account_id=account_id,
                 context={"platform": platform, "solver": "capsolver", "type": "funcaptcha"})
    return None
