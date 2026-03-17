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


def test_response_has_invalid_session_detects_wda_errors():
    assert WDASession._response_has_invalid_session({"error": "invalid session id"})
    assert WDASession._response_has_invalid_session({"message": "Session does not exist"})
    assert not WDASession._response_has_invalid_session({"value": 4})


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


def test_toggle_state_from_attributes_uses_selected_when_value_is_ambiguous():
    session = _make_session()
    state = session._toggle_state_from_attributes("airplane", {"selected": True})
    assert state is True


def test_toggle_state_from_attributes_prefers_value_over_selected_for_airplane():
    session = _make_session()
    state = session._toggle_state_from_attributes("airplane", {"selected": False, "value": "1"})
    assert state is True


def test_element_attribute_uses_private_attribute_accessor():
    session = _make_session()
    with patch.object(session, "_get_element_attribute", return_value="XCUIElementTypeButton") as mock_attr:
        value = session.element_attribute("el-1", "type")

    assert value == "XCUIElementTypeButton"
    mock_attr.assert_called_once_with("el-1", "type")


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


def test_open_control_center_retries_until_toggle_visible():
    session = _make_session()
    session._stabilize_home_for_system_gesture = MagicMock()
    session.swipe = MagicMock()
    session._control_center_is_visible = MagicMock(side_effect=[False, True])

    with patch("time.sleep"):
        ok = session._open_control_center()

    assert ok is True
    assert session.swipe.call_count == 2
    assert session._stabilize_home_for_system_gesture.call_count == 2


def test_open_control_center_reconnects_after_invalid_session():
    session = _make_session()
    session._stabilize_home_for_system_gesture = MagicMock()
    session.swipe = MagicMock()
    session.reconnect = MagicMock(return_value=True)
    session._control_center_is_visible = MagicMock(side_effect=[RuntimeError("invalid session id"), True])

    with patch("time.sleep"):
        ok = session._open_control_center()

    assert ok is True
    session.reconnect.assert_called_once_with(attempts=1, delay_s=0.5)


def test_set_control_center_toggle_reopens_control_center_when_toggle_missing():
    session = _make_session()
    session.element_click = MagicMock()
    session._open_control_center = MagicMock(return_value=True)
    session._read_control_center_toggle_state = MagicMock(side_effect=[
        (None, None),
        ({"ELEMENT": "toggle-1"}, True),
        ({"ELEMENT": "toggle-1"}, False),
    ])

    with patch("time.sleep"):
        ok = session._set_control_center_toggle("wifi", desired_on=False)

    assert ok is True
    session._open_control_center.assert_called_once_with()
    session.element_click.assert_called_once_with("toggle-1")


def test_set_control_center_toggle_reconnects_after_invalid_session():
    session = _make_session()
    session.element_click = MagicMock()
    session.reconnect = MagicMock(return_value=True)
    session._open_control_center = MagicMock(return_value=True)
    session._read_control_center_toggle_state = MagicMock(side_effect=[
        RuntimeError("invalid session id"),
        ({"ELEMENT": "toggle-1"}, True),
        ({"ELEMENT": "toggle-1"}, False),
    ])

    with patch("time.sleep"):
        ok = session._set_control_center_toggle("airplane", desired_on=False)

    assert ok is True
    session.reconnect.assert_called_once_with(attempts=1, delay_s=0.5)
    session._open_control_center.assert_called_once_with()
    session.element_click.assert_called_once_with("toggle-1")


def test_toggle_airplane_mode_reconnects_before_postcheck():
    session = _make_session()
    session.ensure_airplane_mode_off = MagicMock(side_effect=[True, True])
    session.ensure_wifi_off = MagicMock(return_value=True)
    session._open_control_center = MagicMock(return_value=True)
    session._close_control_center = MagicMock()
    session._set_control_center_toggle = MagicMock(side_effect=[True, True])
    session.reconnect = MagicMock(return_value=True)
    session.reset_to_home = MagicMock()

    with patch("time.sleep"):
        ok = session.toggle_airplane_mode(wait_after=0.0)

    assert ok is True
    session.reconnect.assert_called_once_with()
    session.reset_to_home.assert_called_once_with()


def test_toggle_airplane_mode_restores_cellular_only():
    session = _make_session()
    session.ensure_airplane_mode_off = MagicMock(side_effect=[True, True])
    session.ensure_wifi_off = MagicMock(return_value=True)
    session._open_control_center = MagicMock(return_value=True)
    session._close_control_center = MagicMock()
    session._set_control_center_toggle = MagicMock(side_effect=[True, True])
    session.reconnect = MagicMock(return_value=True)
    session.reset_to_home = MagicMock()

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
    session._open_control_center = MagicMock(return_value=True)
    session._close_control_center = MagicMock()
    session._set_control_center_toggle = MagicMock(side_effect=[True, True])
    session.reconnect = MagicMock(return_value=True)

    with patch("time.sleep"):
        ok = session.toggle_airplane_mode(wait_after=0.0)

    assert ok is False
