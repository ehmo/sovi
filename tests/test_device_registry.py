"""Tests for device_registry — DB-driven device management and WDADevice construction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sovi.device.device_registry import (
    get_active_devices,
    get_device_by_id,
    get_device_by_name,
    register_device,
    set_device_status,
    to_wda_device,
    update_heartbeat,
)
from sovi.device.wda_client import WDADevice


# Patch targets — device_registry imports from sovi.db
_SYNC_EXEC = "sovi.device.device_registry.sync_execute"
_SYNC_EXEC_ONE = "sovi.device.device_registry.sync_execute_one"


# --- to_wda_device ---


class TestToWdaDevice:
    def test_aliased_row(self):
        """Column aliasing: label→name, appium_port→wda_port (from get_active_devices)."""
        row = {
            "id": "dev-1",
            "name": "phone-1",
            "udid": "abc123def456",
            "wda_port": 8100,
            "status": "available",
        }
        device = to_wda_device(row)
        assert isinstance(device, WDADevice)
        assert device.name == "phone-1"
        assert device.udid == "abc123def456"
        assert device.wda_port == 8100

    def test_raw_row(self):
        """Raw DB rows use label and appium_port columns."""
        row = {
            "id": "dev-2",
            "label": "phone-2",
            "udid": "xyz789",
            "appium_port": 8200,
        }
        device = to_wda_device(row)
        assert device.name == "phone-2"
        assert device.wda_port == 8200

    def test_fallback_to_udid_truncated(self):
        """When neither name nor label is present, uses udid[:12]."""
        row = {
            "udid": "abcdef123456789",
        }
        device = to_wda_device(row)
        assert device.name == "abcdef123456"

    def test_default_wda_port(self):
        """When neither wda_port nor appium_port is set, defaults to 8100."""
        row = {
            "udid": "abc123",
        }
        device = to_wda_device(row)
        assert device.wda_port == 8100

    def test_base_url_constructed_correctly(self):
        """Verify the WDADevice.base_url is correct after conversion."""
        row = {
            "name": "test",
            "udid": "abc",
            "wda_port": 8300,
        }
        device = to_wda_device(row)
        assert device.base_url == "http://localhost:8300"

    def test_name_preferred_over_label(self):
        """When both name and label exist, name takes precedence."""
        row = {
            "name": "aliased-name",
            "label": "raw-label",
            "udid": "abc",
            "wda_port": 8100,
        }
        device = to_wda_device(row)
        assert device.name == "aliased-name"

    def test_label_used_when_name_is_none(self):
        """When name is None, falls back to label."""
        row = {
            "name": None,
            "label": "raw-label",
            "udid": "abc",
            "wda_port": 8100,
        }
        device = to_wda_device(row)
        assert device.name == "raw-label"


# --- get_active_devices ---


class TestGetActiveDevices:
    def test_returns_list_of_dicts(self):
        devices = [
            {"id": "d1", "name": "phone-1", "udid": "u1", "wda_port": 8100, "status": "available"},
            {"id": "d2", "name": "phone-2", "udid": "u2", "wda_port": 8200, "status": "in_use"},
        ]
        with patch(_SYNC_EXEC, return_value=devices):
            result = get_active_devices()
        assert len(result) == 2
        assert result[0]["name"] == "phone-1"

    def test_returns_empty_when_no_devices(self):
        with patch(_SYNC_EXEC, return_value=[]):
            result = get_active_devices()
        assert result == []

    def test_query_uses_column_aliases(self):
        """Verify the SQL query aliases label→name and appium_port→wda_port."""
        with patch(_SYNC_EXEC, return_value=[]) as mock_exec:
            get_active_devices()
        query = mock_exec.call_args[0][0]
        assert "label AS name" in query
        assert "appium_port AS wda_port" in query
        assert "last_heartbeat AS connected_since" in query


# --- get_device_by_id / get_device_by_name ---


class TestDeviceLookups:
    def test_get_device_by_id(self):
        device = {"id": "d1", "label": "phone-1", "udid": "u1"}
        with patch(_SYNC_EXEC_ONE, return_value=device):
            result = get_device_by_id("d1")
        assert result["id"] == "d1"

    def test_get_device_by_id_not_found(self):
        with patch(_SYNC_EXEC_ONE, return_value=None):
            result = get_device_by_id("nonexistent")
        assert result is None

    def test_get_device_by_name(self):
        device = {"id": "d1", "label": "phone-1"}
        with patch(_SYNC_EXEC_ONE, return_value=device):
            result = get_device_by_name("phone-1")
        assert result["label"] == "phone-1"


# --- register_device ---


class TestRegisterDevice:
    def test_register_returns_device_row(self):
        row = {"id": "d1", "label": "new-phone", "udid": "u1", "status": "available"}
        with patch(_SYNC_EXEC, return_value=[row]):
            result = register_device("new-phone", "u1", model="iPhone 16", wda_port=8100)
        assert result is not None
        assert result["label"] == "new-phone"

    def test_register_returns_none_on_empty_result(self):
        with patch(_SYNC_EXEC, return_value=[]):
            result = register_device("phone", "u1")
        assert result is None

    def test_register_uses_upsert(self):
        """Verify the SQL uses ON CONFLICT for upsert semantics."""
        with patch(_SYNC_EXEC, return_value=[]) as mock_exec:
            register_device("phone", "u1", model="iPhone", ios_version="18.3", wda_port=8100)
        query = mock_exec.call_args[0][0]
        assert "ON CONFLICT" in query
        assert "RETURNING" in query


# --- update_heartbeat / set_device_status ---


class TestDeviceStatusUpdates:
    def test_update_heartbeat_calls_db(self):
        with patch(_SYNC_EXEC) as mock_exec:
            update_heartbeat("dev-1")
        mock_exec.assert_called_once()
        query = mock_exec.call_args[0][0]
        assert "last_heartbeat" in query
        assert "in_use" in query

    def test_set_device_status(self):
        with patch(_SYNC_EXEC) as mock_exec:
            set_device_status("dev-1", "disconnected")
        mock_exec.assert_called_once()
        params = mock_exec.call_args[0][1]
        assert "disconnected" in params
