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
    def test_normal_row(self):
        """DB rows use name and wda_port columns directly."""
        row = {
            "id": "dev-1",
            "name": "phone-1",
            "udid": "abc123def456",
            "wda_port": 8100,
            "status": "active",
        }
        device = to_wda_device(row)
        assert isinstance(device, WDADevice)
        assert device.name == "phone-1"
        assert device.udid == "abc123def456"
        assert device.wda_port == 8100

    def test_row_with_port(self):
        """DB rows with wda_port."""
        row = {
            "id": "dev-2",
            "name": "phone-2",
            "udid": "xyz789",
            "wda_port": 8200,
        }
        device = to_wda_device(row)
        assert device.name == "phone-2"
        assert device.wda_port == 8200

    def test_fallback_to_udid_truncated(self):
        """When name is not present, uses udid[:12]."""
        row = {
            "udid": "abcdef123456789",
        }
        device = to_wda_device(row)
        assert device.name == "abcdef123456"

    def test_default_wda_port(self):
        """When wda_port is not set, defaults to 8100."""
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

    def test_name_used(self):
        """Name column is used directly."""
        row = {
            "name": "my-phone",
            "udid": "abc",
            "wda_port": 8100,
        }
        device = to_wda_device(row)
        assert device.name == "my-phone"

    def test_name_none_falls_back_to_udid(self):
        """When name is None, falls back to udid[:12]."""
        row = {
            "name": None,
            "udid": "abcdef123456789",
            "wda_port": 8100,
        }
        device = to_wda_device(row)
        assert device.name == "abcdef123456"


# --- get_active_devices ---


class TestGetActiveDevices:
    def test_returns_list_of_dicts(self):
        devices = [
            {"id": "d1", "name": "phone-1", "udid": "u1", "wda_port": 8100, "status": "active"},
            {"id": "d2", "name": "phone-2", "udid": "u2", "wda_port": 8200, "status": "active"},
        ]
        with patch(_SYNC_EXEC, return_value=devices):
            result = get_active_devices()
        assert len(result) == 2
        assert result[0]["name"] == "phone-1"

    def test_returns_empty_when_no_devices(self):
        with patch(_SYNC_EXEC, return_value=[]):
            result = get_active_devices()
        assert result == []

    def test_query_uses_correct_columns(self):
        """Verify the SQL query uses deployed column names directly."""
        with patch(_SYNC_EXEC, return_value=[]) as mock_exec:
            get_active_devices()
        query = mock_exec.call_args[0][0]
        assert "name" in query
        assert "wda_port" in query
        assert "connected_since" in query
        assert "status = 'active'" in query


# --- get_device_by_id / get_device_by_name ---


class TestDeviceLookups:
    def test_get_device_by_id(self):
        device = {"id": "d1", "name": "phone-1", "udid": "u1"}
        with patch(_SYNC_EXEC_ONE, return_value=device):
            result = get_device_by_id("d1")
        assert result["id"] == "d1"

    def test_get_device_by_id_not_found(self):
        with patch(_SYNC_EXEC_ONE, return_value=None):
            result = get_device_by_id("nonexistent")
        assert result is None

    def test_get_device_by_name(self):
        device = {"id": "d1", "name": "phone-1"}
        with patch(_SYNC_EXEC_ONE, return_value=device):
            result = get_device_by_name("phone-1")
        assert result["name"] == "phone-1"


# --- register_device ---


class TestRegisterDevice:
    def test_register_returns_device_row(self):
        row = {"id": "d1", "name": "new-phone", "udid": "u1", "status": "active"}
        with patch(_SYNC_EXEC, return_value=[row]):
            result = register_device("new-phone", "u1", model="iPhone 16", wda_port=8100)
        assert result is not None
        assert result["name"] == "new-phone"

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
        assert "connected_since" in query
        assert "'active'" in query

    def test_set_device_status(self):
        with patch(_SYNC_EXEC) as mock_exec:
            set_device_status("dev-1", "disconnected")
        mock_exec.assert_called_once()
        params = mock_exec.call_args[0][1]
        assert "disconnected" in params
