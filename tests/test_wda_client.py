"""Tests for WDA client data structures and helpers."""

from __future__ import annotations

from sovi.device.wda_client import WDADevice


def test_wda_device_base_url():
    dev = WDADevice(name="test", udid="abc123", wda_port=8100)
    assert dev.base_url == "http://localhost:8100"


def test_wda_device_base_url_custom_port():
    dev = WDADevice(name="test", udid="abc123", wda_port=8200)
    assert dev.base_url == "http://localhost:8200"


def test_wda_default_screen_size():
    """Default screen fallback should be iPhone 16 dimensions."""
    from sovi.device.wda_client import WDASession
    assert WDASession._DEFAULT_SCREEN == {"width": 393, "height": 852}
