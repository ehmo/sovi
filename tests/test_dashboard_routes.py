"""Tests for dashboard routes — basic contract tests verifying error handling patterns.

Uses FastAPI TestClient to test routes through the app, avoiding circular imports.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch

# Stub numpy before dashboard imports pull in scheduler->seeder chain
if "numpy" not in sys.modules:
    _np = ModuleType("numpy")
    _np.array = lambda *a, **k: None  # type: ignore[attr-defined]
    _np.ndarray = type  # type: ignore[attr-defined]
    _np.float64 = float  # type: ignore[attr-defined]
    _np.int64 = int  # type: ignore[attr-defined]
    _np.zeros = lambda *a, **k: []  # type: ignore[attr-defined]
    sys.modules["numpy"] = _np

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from sovi.dashboard.app import app  # noqa: E402


@pytest.fixture
def transport():
    """ASGI transport for testing without lifespan (no real DB pool)."""
    return ASGITransport(app=app)


@pytest.fixture
async def client(transport):
    """Async test client."""
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- Device API ---


class TestDeviceAPI:
    async def test_list_devices(self, client):
        mock_devices = [{"id": "d1", "name": "phone-1", "udid": "u1"}]
        with patch("sovi.dashboard.routes.devices.async_get_devices", new_callable=AsyncMock, return_value=mock_devices):
            resp = await client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "phone-1"

    async def test_get_device_found(self, client):
        device = {"id": "d1", "name": "phone-1"}
        with patch("sovi.dashboard.routes.devices.async_get_device", new_callable=AsyncMock, return_value=device):
            resp = await client.get("/api/devices/d1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "d1"

    async def test_get_device_not_found(self, client):
        with patch("sovi.dashboard.routes.devices.async_get_device", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/api/devices/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_register_device(self, client):
        result_row = {"id": "d1", "name": "new-phone", "status": "active"}
        with patch("sovi.dashboard.routes.devices.async_register_device", new_callable=AsyncMock, return_value=result_row):
            resp = await client.post("/api/devices", json={
                "name": "new-phone",
                "udid": "u1",
                "model": "iPhone",
                "ios_version": "18.3",
                "wda_port": 8100,
            })
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-phone"

    async def test_register_device_failure(self, client):
        with patch("sovi.dashboard.routes.devices.async_register_device", new_callable=AsyncMock, return_value=None):
            resp = await client.post("/api/devices", json={
                "name": "fail",
                "udid": "u1",
            })
        assert resp.status_code == 200
        assert "error" in resp.json()


# --- Account API ---


class TestAccountAPI:
    async def test_get_account_not_found(self, client):
        with patch("sovi.dashboard.routes.accounts.execute_one", new_callable=AsyncMock, return_value=None):
            resp = await client.get("/api/accounts/nonexistent")
        assert resp.status_code == 404

    async def test_get_account_found(self, client):
        account = {"id": "a1", "platform": "tiktok", "username": "user1"}
        events_list = [{"id": "e1", "event_type": "warming_complete"}]

        call_count = [0]

        async def mock_execute_one(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return account
            return None

        with (
            patch("sovi.dashboard.routes.accounts.execute_one", side_effect=mock_execute_one),
            patch("sovi.dashboard.routes.accounts.execute", new_callable=AsyncMock, return_value=events_list),
        ):
            resp = await client.get("/api/accounts/a1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["platform"] == "tiktok"
        assert "recent_events" in data

    async def test_create_account_bad_niche(self, client):
        with patch("sovi.dashboard.routes.accounts.execute_one", new_callable=AsyncMock, return_value=None):
            resp = await client.post("/api/accounts", json={
                "platform": "tiktok",
                "username": "u1",
                "niche_slug": "nonexistent",
            })
        assert resp.status_code == 400

    async def test_update_account_invalid_state(self, client):
        resp = await client.patch("/api/accounts/a1", json={
            "current_state": "totally_invalid",
        })
        assert resp.status_code == 400

    async def test_update_account_no_fields(self, client):
        resp = await client.patch("/api/accounts/a1", json={})
        assert resp.status_code == 200
        assert "error" in resp.json()


# --- Error handling pattern verification ---


class TestErrorHandlingPatterns:
    """Verify routes use proper HTTPException patterns, not tuple returns."""

    def test_device_route_uses_httpexception(self):
        """Import and inspect source to confirm HTTPException usage."""
        import sovi.dashboard.routes.devices as dev_mod
        import inspect

        source = inspect.getsource(dev_mod.get_device)
        assert "HTTPException" in source
        assert "return (404" not in source

    def test_account_route_uses_httpexception(self):
        import sovi.dashboard.routes.accounts as acc_mod
        import inspect

        source = inspect.getsource(acc_mod.get_account)
        assert "HTTPException" in source

        source_create = inspect.getsource(acc_mod.create_account)
        assert "HTTPException" in source_create
        assert "400" in source_create

    def test_account_update_validates_state(self):
        import sovi.dashboard.routes.accounts as acc_mod
        import inspect

        source = inspect.getsource(acc_mod.update_account)
        assert "AccountState" in source
        assert "HTTPException" in source
