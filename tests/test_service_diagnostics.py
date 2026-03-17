from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx

from sovi.cli import health_check
from sovi.device.service_diagnostics import (
    WDAFailureDiagnostic,
    device_service_slug,
    diagnose_wda_failure,
)


def test_device_service_slug_normalizes_names():
    assert device_service_slug(" iPhone A ") == "iphone-a"


def test_diagnose_wda_failure_detects_untrusted_certificate(tmp_path, monkeypatch):
    log_path = tmp_path / "com.sovi.wda-iphone-a.err"
    log_path.write_text(
        "The application could not be launched because the Developer App Certificate "
        "is not trusted.\n"
    )
    monkeypatch.setattr(
        "sovi.device.service_diagnostics.wda_error_log_path",
        lambda _device_name: Path(log_path),
    )

    diagnostic = diagnose_wda_failure("iPhone-A")

    assert diagnostic is not None
    assert diagnostic.reason == "wda_certificate_untrusted"
    assert diagnostic.manual_action_required is True
    assert diagnostic.retry_after_seconds == 900
    assert "Trust the developer certificate" in diagnostic.hint


def test_diagnose_wda_failure_reads_log_head_for_early_certificate_error(tmp_path, monkeypatch):
    log_path = tmp_path / "com.sovi.wda-iphone-a.err"
    log_path.write_text(
        "The application could not be launched because the Developer App Certificate is not trusted.\n"
        + ("later noise\n" * 12000)
    )
    monkeypatch.setattr(
        "sovi.device.service_diagnostics.wda_error_log_path",
        lambda _device_name: Path(log_path),
    )

    diagnostic = diagnose_wda_failure("iPhone-A")

    assert diagnostic is not None
    assert diagnostic.reason == "wda_certificate_untrusted"


def test_health_check_surfaces_wda_diagnostic(capsys):
    diagnostic = WDAFailureDiagnostic(
        reason="wda_certificate_untrusted",
        summary="WDA cannot launch on iPhone-A because the developer certificate is not trusted",
        hint="Trust the developer certificate on iPhone-A, then reload com.sovi.wda-iphone-a",
        log_path="/tmp/com.sovi.wda-iphone-a.err",
        launchd_label="com.sovi.wda-iphone-a",
        retry_after_seconds=900,
        manual_action_required=True,
    )

    with (
        patch.object(health_check, "DEVICES", [{"name": "iPhone-A", "wda_port": 8100}]),
        patch.object(health_check.httpx, "get", side_effect=httpx.ConnectError("down")),
        patch.object(health_check, "diagnose_wda_failure", return_value=diagnostic),
    ):
        health_check.check_wda()

    out = capsys.readouterr().out
    assert "WDA cannot launch on iPhone-A" in out
    assert "Trust the developer certificate on iPhone-A" in out
