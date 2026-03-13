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


# --- Local puzzle CAPTCHA solver (no API needed) ---


def detect_captcha_popup(
    screenshot_png: bytes,
    *,
    scale: int = 3,
) -> dict | None:
    """Detect TikTok's puzzle CAPTCHA popup and return its geometry.

    The popup has a dimmed overlay (~128 brightness at screen edges) with a
    white popup in the center. Returns popup and slider positions in WDA points,
    or None if no CAPTCHA popup detected.
    """
    try:
        import numpy as np
        from PIL import Image
        import io
    except ImportError:
        return None

    if not screenshot_png:
        return None

    try:
        img = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
        arr = np.array(img, dtype=np.float32)
        h, w = arr.shape[:2]

        # Detect dimmed overlay: edge pixels should be ~128 brightness
        edge_samples = [arr[y, 50, :].mean() for y in range(int(h * 0.2), int(h * 0.8), int(h * 0.1))]
        if not all(110 < b < 150 for b in edge_samples):
            return None  # No dimmed overlay = no CAPTCHA popup

        # Find popup vertical bounds: center column transitions from overlay to white
        center_x = w // 2
        popup_top = popup_bottom = None
        for y in range(int(h * 0.2), int(h * 0.8)):
            if arr[y, center_x, :].mean() > 250 and popup_top is None:
                popup_top = y
            if popup_top and arr[y, center_x, :].mean() > 250:
                popup_bottom = y

        if not popup_top or not popup_bottom:
            return None

        # Find popup horizontal bounds
        mid_y = (popup_top + popup_bottom) // 2
        popup_left = popup_right = None
        for x in range(50, w // 2):
            if arr[mid_y, x, :].mean() > 240:
                popup_left = x
                break
        for x in range(w - 50, w // 2, -1):
            if arr[mid_y, x, :].mean() > 240:
                popup_right = x
                break

        if not popup_left or not popup_right:
            return None

        # Find photo area within popup: where center brightness drops below 230
        photo_top = photo_bottom = None
        for y in range(popup_top, popup_bottom):
            center_bright = arr[y, center_x, :].mean()
            if center_bright < 230 and photo_top is None:
                photo_top = y
            if photo_top and center_bright < 230:
                photo_bottom = y

        if not photo_top or not photo_bottom:
            return None

        # Slider track: white area below photo (y > photo_bottom)
        slider_y = None
        for y in range(photo_bottom + 5, min(photo_bottom + 200, h)):
            if arr[y, center_x, :].mean() > 230:
                slider_y = y + 30  # Center of slider track area
                break

        if not slider_y:
            slider_y = photo_bottom + 50  # Fallback estimate

        return {
            "popup": (popup_left // scale, popup_top // scale,
                      popup_right // scale, popup_bottom // scale),
            "photo": (popup_left // scale, photo_top // scale,
                      popup_right // scale, photo_bottom // scale),
            "slider_y": slider_y // scale,
            "slider_start_x": popup_left // scale + 10,
            "popup_width": (popup_right - popup_left) // scale,
        }

    except Exception:
        logger.debug("Popup detection failed", exc_info=True)
        return None


def solve_puzzle_local(
    screenshot_png: bytes,
    *,
    scale: int = 3,
) -> dict | None:
    """Detect TikTok's puzzle CAPTCHA and return slider drag targets.

    Returns a list of drag attempts (percentage of popup width) to try,
    ordered by most likely position based on edge detection.
    Falls back to a fixed set of common positions if detection fails.

    Returns dict with:
        {"slider_y": int, "slider_start_x": int, "targets": list[float],
         "popup_width": int}
    or None if no CAPTCHA popup detected.

    The caller should try each target percentage:
        target_x = slider_start_x + int(popup_width * target)
        drag from (slider_start_x, slider_y) to (target_x, slider_y)
    """
    try:
        import numpy as np
        from PIL import Image
        import io
    except ImportError:
        logger.error("numpy/PIL required for local puzzle solver")
        return None

    popup = detect_captcha_popup(screenshot_png, scale=scale)
    if not popup:
        return None

    photo_left = popup["photo"][0] * scale
    photo_top = popup["photo"][1] * scale
    photo_right = popup["photo"][2] * scale
    photo_bottom = popup["photo"][3] * scale

    # Default target percentages (empirically effective for TikTok puzzles)
    default_targets = [0.45, 0.50, 0.55, 0.60, 0.40, 0.65, 0.35, 0.70]

    try:
        img = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
        arr = np.array(img, dtype=np.float32)
        gray = arr.mean(axis=2)

        # Crop to photo region for edge analysis
        photo_crop = gray[photo_top:photo_bottom, photo_left:photo_right]
        ph, pw = photo_crop.shape

        if pw < 100 or ph < 50:
            logger.debug("Photo crop too small: %dx%d", pw, ph)
            return {**popup, "targets": default_targets}

        # Horizontal gradient for piece/cutout edge detection
        grad = np.abs(np.diff(photo_crop, axis=1))
        col_grad = grad.mean(axis=0)

        kernel_size = min(15, len(col_grad) // 4)
        if kernel_size >= 3:
            kernel = np.ones(kernel_size) / kernel_size
            col_grad_smooth = np.convolve(col_grad, kernel, mode="same")

            # Find peaks
            threshold = np.median(col_grad_smooth) * 1.5
            peaks = []
            in_peak = False
            peak_start = 0
            for i, v in enumerate(col_grad_smooth):
                if v > threshold and not in_peak:
                    in_peak = True
                    peak_start = i
                elif v <= threshold and in_peak:
                    in_peak = False
                    peaks.append(((peak_start + i) // 2,
                                  float(col_grad_smooth[peak_start:i].max())))

            # Filter borders, find strongest left/right peaks
            margin = int(pw * 0.06)
            interior = [(c, s) for c, s in peaks if margin < c < pw - margin]

            if len(interior) >= 2:
                left_peaks = [(c, s) for c, s in interior if c < pw * 0.4]
                right_peaks = [(c, s) for c, s in interior if c > pw * 0.4]

                if left_peaks and right_peaks:
                    piece = max(left_peaks, key=lambda p: p[1])
                    cutout = max(right_peaks, key=lambda p: p[1])

                    # Target = cutout position as fraction of popup width
                    cutout_pct = cutout[0] / pw
                    piece_pct = piece[0] / pw
                    # The drag distance maps to (cutout - piece) position
                    drag_pct = cutout_pct - piece_pct

                    # Put the detected target first, then nearby positions
                    detected_targets = [
                        drag_pct,
                        drag_pct + 0.05,
                        drag_pct - 0.05,
                        drag_pct + 0.10,
                        drag_pct - 0.10,
                    ]
                    # Filter valid range and add defaults
                    targets = [t for t in detected_targets if 0.2 < t < 0.8]
                    targets.extend(t for t in default_targets if t not in targets)

                    logger.info(
                        "Puzzle solver: piece=%.0f%% cutout=%.0f%% drag=%.0f%%",
                        piece_pct * 100, cutout_pct * 100, drag_pct * 100,
                    )

                    return {**popup, "targets": targets}

    except Exception:
        logger.debug("Edge analysis failed, using defaults", exc_info=True)

    return {**popup, "targets": default_targets}
