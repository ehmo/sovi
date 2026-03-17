"""Local launchd/log diagnostics for per-device studio services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WDAFailureDiagnostic:
    """Actionable classification for a failed WDA launch."""

    reason: str
    summary: str
    hint: str
    log_path: str
    launchd_label: str
    retry_after_seconds: int
    manual_action_required: bool = False


@dataclass(frozen=True)
class _DiagnosticPattern:
    reason: str
    needles: tuple[str, ...]
    summary_template: str
    hint_template: str
    retry_after_seconds: int = 60
    manual_action_required: bool = False


_WDA_PATTERNS = (
    _DiagnosticPattern(
        reason="wda_certificate_untrusted",
        needles=(
            "developer app certificate is not trusted",
            "the application could not be launched because the developer app certificate is not trusted",
        ),
        summary_template=(
            "WDA cannot launch on {device_name} because the developer certificate is not trusted"
        ),
        hint_template=(
            "Trust the developer certificate on {device_name}, then reload {launchd_label}. "
            "See {log_path}"
        ),
        retry_after_seconds=900,
        manual_action_required=True,
    ),
    _DiagnosticPattern(
        reason="wda_destination_unavailable",
        needles=(
            "unable to find a destination matching the provided destination specifier",
            "device is not connected",
            "no devices are booted",
            "timed out waiting for all destinations matching the provided destination specifier",
        ),
        summary_template="xcodebuild cannot find {device_name} as a valid WDA destination",
        hint_template=(
            "Check the USB connection, Developer Mode, and reload {launchd_label}. "
            "See {log_path}"
        ),
        retry_after_seconds=120,
    ),
    _DiagnosticPattern(
        reason="wda_xcodebuild_failed",
        needles=(
            "** test execute failed **",
            "** build interrupted **",
            "xcodebuild exited with code 65",
        ),
        summary_template="WDA launch failed inside xcodebuild on {device_name}",
        hint_template="Inspect the WDA stderr log at {log_path} and reload {launchd_label}",
        retry_after_seconds=300,
    ),
)


def device_service_slug(device_name: str) -> str:
    """Normalize device names to the launchd label suffix used in plists/logs."""
    slug = (device_name or "unknown").strip().lower().replace(" ", "-")
    return slug or "unknown"


def wda_launchd_label(device_name: str) -> str:
    return f"com.sovi.wda-{device_service_slug(device_name)}"


def wda_error_log_path(device_name: str) -> Path:
    return Path("/tmp") / f"{wda_launchd_label(device_name)}.err"


def _read_log_excerpt(path: Path, max_bytes: int = 16384) -> str:
    """Read enough of a log to catch both early setup errors and recent failures."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size <= max_bytes * 2:
                handle.seek(0)
                return handle.read().decode("utf-8", errors="replace")

            handle.seek(0)
            head = handle.read(max_bytes)
            handle.seek(max(size - max_bytes, 0))
            tail = handle.read()
            return (
                head.decode("utf-8", errors="replace")
                + "\n...\n"
                + tail.decode("utf-8", errors="replace")
            )
    except OSError:
        return ""


def _extract_detail(log_text: str, needles: tuple[str, ...]) -> str | None:
    for line in reversed(log_text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(needle in lower for needle in needles):
            return stripped
    return None


def diagnose_wda_failure(device_name: str) -> WDAFailureDiagnostic | None:
    """Best-effort classification of the latest WDA launch failure for a device."""
    log_path = wda_error_log_path(device_name)
    log_text = _read_log_excerpt(log_path)
    if not log_text:
        return None

    lower = log_text.lower()
    launchd_label = wda_launchd_label(device_name)

    for pattern in _WDA_PATTERNS:
        if not any(needle in lower for needle in pattern.needles):
            continue

        summary = pattern.summary_template.format(device_name=device_name)
        detail = _extract_detail(log_text, pattern.needles)
        if detail and detail.lower() not in summary.lower():
            summary = f"{summary}: {detail}"

        return WDAFailureDiagnostic(
            reason=pattern.reason,
            summary=summary,
            hint=pattern.hint_template.format(
                device_name=device_name,
                launchd_label=launchd_label,
                log_path=str(log_path),
            ),
            log_path=str(log_path),
            launchd_label=launchd_label,
            retry_after_seconds=pattern.retry_after_seconds,
            manual_action_required=pattern.manual_action_required,
        )

    return None
