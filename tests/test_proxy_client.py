"""Tests for proxy_client — URL building, health check, assignment."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sovi.device.proxy_client import (
    assign_proxy_to_device,
    get_device_proxy,
    health_check_proxy,
    proxy_url,
)

_SYNC_EXEC = "sovi.device.proxy_client.sync_execute"
_SYNC_EXEC_ONE = "sovi.device.proxy_client.sync_execute_one"


@pytest.fixture
def mock_proxy_db():
    """Patch sync_execute/sync_execute_one where proxy_client imports them."""
    mock = MagicMock()
    mock.execute.return_value = []
    mock.execute_one.return_value = None
    with (
        patch(_SYNC_EXEC, side_effect=mock.execute),
        patch(_SYNC_EXEC_ONE, side_effect=mock.execute_one),
    ):
        yield mock


# --- proxy_url ---


class TestProxyUrl:
    def test_no_credentials(self):
        proxy = {"host": "1.2.3.4", "port": 1080}
        assert proxy_url(proxy) == "socks5://1.2.3.4:1080"

    def test_no_credentials_enc_key(self):
        proxy = {"host": "10.0.0.1", "port": 9050, "credentials_enc": None}
        assert proxy_url(proxy) == "socks5://10.0.0.1:9050"

    def test_empty_credentials_enc(self):
        proxy = {"host": "10.0.0.1", "port": 9050, "credentials_enc": ""}
        assert proxy_url(proxy) == "socks5://10.0.0.1:9050"

    def test_with_encrypted_credentials(self):
        proxy = {"host": "5.6.7.8", "port": 1080, "credentials_enc": "encrypted_blob"}
        with patch("sovi.device.proxy_client.decrypt", return_value="user:pass"):
            url = proxy_url(proxy)
        assert url == "socks5://user:pass@5.6.7.8:1080"


# --- get_device_proxy ---


class TestGetDeviceProxy:
    def test_returns_proxy_row(self, mock_proxy_db):
        expected = {"id": "p1", "host": "1.2.3.4", "port": 1080, "is_healthy": True}
        mock_proxy_db.execute_one.return_value = expected
        result = get_device_proxy("dev-1")
        assert result == expected

    def test_returns_none_if_unassigned(self, mock_proxy_db):
        mock_proxy_db.execute_one.return_value = None
        result = get_device_proxy("dev-no-proxy")
        assert result is None


# --- assign_proxy_to_device ---


class TestAssignProxyToDevice:
    def test_successful_assignment(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sync_conn is imported inside the function via `from sovi.db import sync_conn`
        with patch("sovi.db.sync_conn", return_value=mock_conn):
            result = assign_proxy_to_device("proxy-1", "dev-1")
        assert result is True
        assert mock_cursor.execute.call_count == 2
        mock_conn.commit.assert_called_once()

    def test_failure_returns_false(self):
        with patch("sovi.db.sync_conn", side_effect=Exception("DB error")):
            result = assign_proxy_to_device("proxy-1", "dev-1")
        assert result is False


# --- health_check_proxy ---


class TestHealthCheckProxy:
    def test_healthy_proxy(self, mock_proxy_db):
        proxy = {"id": "p1", "host": "1.2.3.4", "port": 1080, "credentials_enc": "enc"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ip": "1.2.3.4"}

        with (
            patch("sovi.device.proxy_client.decrypt", return_value="u:p"),
            patch("httpx.get", return_value=mock_response),
        ):
            result = health_check_proxy(proxy)

        assert result is True
        mock_proxy_db.execute.assert_called_once()
        # call_args[0] = (query, params); params[0] = is_healthy
        params = mock_proxy_db.execute.call_args[0][1]
        assert params[0] is True

    def test_unhealthy_status_code(self, mock_proxy_db):
        proxy = {"id": "p2", "host": "1.2.3.4", "port": 1080}

        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("httpx.get", return_value=mock_response):
            result = health_check_proxy(proxy)

        assert result is False
        mock_proxy_db.execute.assert_called_once()
        params = mock_proxy_db.execute.call_args[0][1]
        assert params[0] is False

    def test_connection_error_marks_unhealthy(self, mock_proxy_db):
        proxy = {"id": "p3", "host": "bad.host", "port": 1080}

        with patch("httpx.get", side_effect=Exception("Connection refused")):
            result = health_check_proxy(proxy)

        assert result is False
        mock_proxy_db.execute.assert_called_once()
