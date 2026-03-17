"""Tests for WDA client data structures and helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovi.device.wda_client import WDADevice, WDASession


def test_wda_device_base_url():
    dev = WDADevice(name="test", udid="abc123", wda_port=8100)
    assert dev.base_url == "http://localhost:8100"


def test_wda_device_base_url_custom_port():
    dev = WDADevice(name="test", udid="abc123", wda_port=8200)
    assert dev.base_url == "http://localhost:8200"


def test_wda_default_screen_size():
    """Default screen fallback should be iPhone 16 dimensions."""
    assert WDASession._DEFAULT_SCREEN == {"width": 393, "height": 852}


def _make_session() -> WDASession:
    session = WDASession(WDADevice(name="test", udid="abc123", wda_port=8100))
    session.session_id = "sess-1"
    return session


def test_toggle_state_from_attributes_detects_wifi_connected():
    session = _make_session()
    state = session._toggle_state_from_attributes("wifi", {"value": "Wi-Fi, Office Network"})
    assert state is True


def test_toggle_state_from_attributes_detects_wifi_off():
    session = _make_session()
    state = session._toggle_state_from_attributes("wifi", {"value": "Wi-Fi"})
    assert state is False


def test_toggle_state_from_attributes_prefers_selected_for_airplane():
    session = _make_session()
    state = session._toggle_state_from_attributes("airplane", {"selected": True, "value": "0"})
    assert state is True


def test_set_control_center_toggle_clicks_until_desired_state():
    session = _make_session()
    session.element_click = MagicMock()
    session._read_control_center_toggle_state = MagicMock(side_effect=[
        ({"ELEMENT": "toggle-1"}, True),
        ({"ELEMENT": "toggle-1"}, False),
    ])

    with patch("time.sleep"):
        ok = session._set_control_center_toggle("airplane", desired_on=False)

    assert ok is True
    session.element_click.assert_called_once_with("toggle-1")


def test_toggle_airplane_mode_restores_cellular_only():
    session = _make_session()
    session.ensure_airplane_mode_off = MagicMock(side_effect=[True, True])
    session.ensure_wifi_off = MagicMock(return_value=True)
    session._open_control_center = MagicMock()
    session._close_control_center = MagicMock()
    session._set_control_center_toggle = MagicMock(side_effect=[True, True])

    with patch("time.sleep"):
        ok = session.toggle_airplane_mode(wait_after=0.0)

    assert ok is True
    assert session.ensure_airplane_mode_off.call_count == 2
    session.ensure_wifi_off.assert_called_once_with()
    assert session._set_control_center_toggle.call_args_list[0].kwargs == {"desired_on": True}
    assert session._set_control_center_toggle.call_args_list[1].kwargs == {"desired_on": False}


def test_toggle_airplane_mode_fails_if_postcheck_cannot_restore_cellular_only():
    session = _make_session()
    session.ensure_airplane_mode_off = MagicMock(side_effect=[True, False])
    session.ensure_wifi_off = MagicMock(return_value=True)
    session._open_control_center = MagicMock()
    session._close_control_center = MagicMock()
    session._set_control_center_toggle = MagicMock(side_effect=[True, True])

    with patch("time.sleep"):
        ok = session.toggle_airplane_mode(wait_after=0.0)

    assert ok is False
