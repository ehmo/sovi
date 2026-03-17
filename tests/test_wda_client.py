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


def test_toggle_state_from_attributes_detects_cellular_on():
    session = _make_session()
    state = session._toggle_state_from_attributes("cellular", {"label": "Cellular Data, LTE"})
    assert state is True


def test_toggle_state_from_attributes_detects_cellular_off():
    session = _make_session()
    state = session._toggle_state_from_attributes("cellular", {"label": "Mobile Data Off"})
    assert state is False


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


def test_ensure_cellular_data_on_uses_control_center_toggle():
    session = _make_session()
    session._open_control_center = MagicMock(return_value=True)
    session._set_control_center_toggle = MagicMock(return_value=True)
    session._close_control_center = MagicMock()

    ok = session.ensure_cellular_data_on()

    assert ok is True
    session._set_control_center_toggle.assert_called_once_with("cellular", desired_on=True)
    session._close_control_center.assert_called_once_with()


def test_probe_cellular_connectivity_returns_true_on_success_page():
    session = _make_session()
    session.open_url = MagicMock()
    session.source = MagicMock(return_value="<html><title>Success</title><body>Success</body></html>")
    session.reset_to_home = MagicMock()

    with patch("time.sleep"), patch("time.time_ns", return_value=1234):
        ok = session.probe_cellular_connectivity(attempts=1, wait_s=0.0)

    assert ok is True
    session.open_url.assert_called_once_with("http://captive.apple.com/hotspot-detect.html?_=1234")
    session.reset_to_home.assert_called_once_with()


def test_probe_cellular_connectivity_reconnects_after_invalid_session():
    session = _make_session()
    session.open_url = MagicMock()
    session.source = MagicMock(side_effect=[RuntimeError("invalid session id"), "<html>Success</html>"])
    session.reconnect = MagicMock(return_value=True)
    session.reset_to_home = MagicMock()

    with patch("time.sleep"), patch("time.time_ns", side_effect=[1111, 2222]):
        ok = session.probe_cellular_connectivity(attempts=2, wait_s=0.0)

    assert ok is True
    session.reconnect.assert_called_once_with(attempts=1, delay_s=0.5)
    assert session.open_url.call_args_list[0].args == ("http://captive.apple.com/hotspot-detect.html?_=1111",)
    assert session.open_url.call_args_list[1].args == ("http://captive.apple.com/hotspot-detect.html?_=2222",)
    session.reset_to_home.assert_called_once_with()


def test_reset_cellular_data_connection_turns_data_off_then_on_and_proves_recovery():
    session = _make_session()
    session.ensure_airplane_mode_off = MagicMock(return_value=True)
    session.ensure_wifi_off = MagicMock(return_value=True)
    session.set_cellular_data_enabled = MagicMock(side_effect=[True, True])
    session.reconnect = MagicMock(return_value=True)
    session.ensure_cellular_only = MagicMock(return_value=True)
    session.probe_cellular_connectivity = MagicMock(return_value=True)
    session.reset_to_home = MagicMock()

    with patch("time.sleep") as mock_sleep:
        ok = session.reset_cellular_data_connection(wait_off_seconds=60.0, recovery_wait_s=0.0)

    assert ok is True
    assert session.set_cellular_data_enabled.call_args_list[0].args == (False,)
    assert session.set_cellular_data_enabled.call_args_list[1].args == (True,)
    assert any(call.args == (60.0,) for call in mock_sleep.call_args_list)
    session.probe_cellular_connectivity.assert_called_once_with(attempts=4, wait_s=5.0, cleanup=False)
    session.reset_to_home.assert_called_once_with()


def test_toggle_airplane_mode_uses_cellular_reset_flow():
    session = _make_session()
    session.reset_cellular_data_connection = MagicMock(return_value=True)

    ok = session.toggle_airplane_mode(wait_after=0.0)

    assert ok is True
    session.reset_cellular_data_connection.assert_called_once_with(
        wait_off_seconds=60.0,
        recovery_wait_s=0.0,
    )


def test_ensure_cellular_ready_requires_probe_success():
    session = _make_session()
    session.ensure_cellular_only = MagicMock(return_value=True)
    session.probe_cellular_connectivity = MagicMock(return_value=False)

    ok = session.ensure_cellular_ready(probe_attempts=2, probe_wait_s=1.5, cleanup=True)

    assert ok is False
    session.ensure_cellular_only.assert_called_once_with()
    session.probe_cellular_connectivity.assert_called_once_with(
        attempts=2,
        wait_s=1.5,
        cleanup=True,
    )
