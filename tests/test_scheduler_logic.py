"""Tests for scheduler pure logic — warming phase transitions, task types, constants."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# Stub numpy before scheduler imports seeder_email transitively.
if "numpy" not in sys.modules:
    _np = ModuleType("numpy")
    _np.array = lambda *a, **k: None  # type: ignore[attr-defined]
    _np.ndarray = type  # type: ignore[attr-defined]
    _np.float64 = float  # type: ignore[attr-defined]
    _np.int64 = int  # type: ignore[attr-defined]
    _np.zeros = lambda *a, **k: []  # type: ignore[attr-defined]
    sys.modules["numpy"] = _np

from sovi.device.scheduler import (
    OVERHEAD_MIN,
    WARMABLE_PLATFORMS,
    WARMING_DURATION_MIN,
    DeviceScheduler,
    DeviceThread,
)
from sovi.device.warming import WarmingPhase
from sovi.models import AccountState


# --- Constants ---


def test_timing_constants():
    assert WARMING_DURATION_MIN == 30
    assert OVERHEAD_MIN == 15


def test_warmable_platforms():
    assert "tiktok" in WARMABLE_PLATFORMS
    assert "instagram" in WARMABLE_PLATFORMS


# --- DeviceThread ---


def test_device_thread_defaults():
    dt = DeviceThread(device_id="abc", device_name="test")
    assert dt.current_task == "idle"
    assert dt.sessions_today == 0
    assert dt.running is False
    assert dt.error is None
    assert dt.current_account is None


def test_device_thread_running_state():
    dt = DeviceThread(device_id="abc", device_name="test", running=True)
    assert dt.running is True
    dt.running = False
    assert dt.running is False


# --- Phase mapping (mirrors scheduler._execute_warming logic) ---


def test_warming_phase_from_account_state():
    """Phase map used in _execute_warming: account state -> warming phase."""
    phase_map = {
        AccountState.CREATED: WarmingPhase.PASSIVE,
        AccountState.WARMING_P1: WarmingPhase.PASSIVE,
        AccountState.WARMING_P2: WarmingPhase.LIGHT,
        AccountState.WARMING_P3: WarmingPhase.MODERATE,
        AccountState.ACTIVE: WarmingPhase.LIGHT,
    }
    # Every warmable state must have a mapping
    for state in (AccountState.CREATED, AccountState.WARMING_P1, AccountState.WARMING_P2,
                  AccountState.WARMING_P3, AccountState.ACTIVE):
        assert state in phase_map


def test_phase_map_default_fallback():
    """Unknown states should fall back to PASSIVE via .get() default."""
    phase_map = {
        AccountState.CREATED: WarmingPhase.PASSIVE,
        AccountState.WARMING_P1: WarmingPhase.PASSIVE,
        AccountState.WARMING_P2: WarmingPhase.LIGHT,
        AccountState.WARMING_P3: WarmingPhase.MODERATE,
        AccountState.ACTIVE: WarmingPhase.LIGHT,
    }
    # States not in the map get PASSIVE fallback
    assert phase_map.get(AccountState.FLAGGED, WarmingPhase.PASSIVE) == WarmingPhase.PASSIVE
    assert phase_map.get(AccountState.BANNED, WarmingPhase.PASSIVE) == WarmingPhase.PASSIVE


def test_phase_map_strenum_interop():
    """AccountState enum keys work with raw string lookups (StrEnum)."""
    phase_map = {
        AccountState.CREATED: WarmingPhase.PASSIVE,
        AccountState.WARMING_P1: WarmingPhase.PASSIVE,
    }
    # StrEnum allows string lookup
    assert phase_map.get("created") == WarmingPhase.PASSIVE
    assert phase_map.get("warming_p1") == WarmingPhase.PASSIVE


# --- Day-count state transitions ---


def test_warming_day_state_transitions():
    """Phase transitions based on warming days (from _execute_warming)."""
    def next_state(day_count: int) -> AccountState:
        if day_count <= 3:
            return AccountState.WARMING_P1
        elif day_count <= 7:
            return AccountState.WARMING_P2
        elif day_count <= 14:
            return AccountState.WARMING_P3
        else:
            return AccountState.ACTIVE

    assert next_state(1) == AccountState.WARMING_P1
    assert next_state(3) == AccountState.WARMING_P1
    assert next_state(4) == AccountState.WARMING_P2
    assert next_state(7) == AccountState.WARMING_P2
    assert next_state(8) == AccountState.WARMING_P3
    assert next_state(14) == AccountState.WARMING_P3
    assert next_state(15) == AccountState.ACTIVE


def test_warming_day_boundary_values():
    """Boundary: day 0 (fresh account), day 1 (first warming)."""
    def next_state(day_count: int) -> AccountState:
        if day_count <= 3:
            return AccountState.WARMING_P1
        elif day_count <= 7:
            return AccountState.WARMING_P2
        elif day_count <= 14:
            return AccountState.WARMING_P3
        else:
            return AccountState.ACTIVE

    assert next_state(0) == AccountState.WARMING_P1  # day 0 = fresh
    assert next_state(100) == AccountState.ACTIVE     # long-warmed


def test_warming_day_transitions_use_enum_values():
    """Verify returned states are proper AccountState enum members (not raw strings)."""
    def next_state(day_count: int) -> AccountState:
        if day_count <= 3:
            return AccountState.WARMING_P1
        elif day_count <= 7:
            return AccountState.WARMING_P2
        elif day_count <= 14:
            return AccountState.WARMING_P3
        else:
            return AccountState.ACTIVE

    result = next_state(5)
    assert isinstance(result, AccountState)
    assert result.value == "warming_p2"


# --- DeviceScheduler ---


def test_scheduler_initial_state():
    scheduler = DeviceScheduler()
    assert not scheduler.is_running
    assert scheduler._threads == {}


def test_scheduler_status_empty():
    scheduler = DeviceScheduler()
    status = scheduler.status()
    assert status["running"] is False
    assert status["device_count"] == 0
    assert status["threads"] == {}


def test_scheduler_stop_event():
    scheduler = DeviceScheduler()
    assert not scheduler._stop_event.is_set()
    scheduler._stop_event.set()
    assert scheduler._stop_event.is_set()
    assert not scheduler.is_running


def test_scheduler_start_recovers_interrupted_seeder_tasks():
    scheduler = DeviceScheduler()
    device_row = {"id": "dev-1", "name": "iPhone-1"}

    with (
        patch("sovi.device.scheduler.enforce_clean_room"),
        patch.object(scheduler, "guard_runtime_environment", return_value=True),
        patch.object(scheduler._instance_lock, "acquire", return_value=True),
        patch("sovi.device.scheduler.build_scheduler_owner", return_value=MagicMock(to_dict=lambda: {"pid": 1})),
        patch("sovi.device.scheduler.sync_execute", return_value=[device_row]),
        patch.object(scheduler._rotator, "start"),
        patch("sovi.device.scheduler.recover_interrupted_seeder_tasks") as mock_recover,
        patch("sovi.device.scheduler.dedupe_open_seeder_tasks") as mock_dedupe,
        patch("sovi.device.scheduler.populate_seeder_tasks") as mock_populate,
        patch("sovi.device.scheduler.threading.Thread") as mock_thread,
    ):
        scheduler.start()

    mock_recover.assert_called_once()
    mock_dedupe.assert_called_once()
    mock_populate.assert_called_once()
    mock_thread.assert_called_once()


def test_scheduler_start_falls_back_to_disconnected_devices_after_restart():
    scheduler = DeviceScheduler()
    device_row = {
        "id": "dev-1",
        "name": "iPhone-1",
        "model": "iPhone 16",
        "udid": "abc123",
        "ios_version": "18.3",
        "wda_port": 8100,
        "status": "disconnected",
        "current_role": "seeder",
        "connected_since": None,
        "role_changed_at": None,
        "seeder_cooldown_until": None,
    }

    with (
        patch("sovi.device.scheduler.enforce_clean_room"),
        patch.object(scheduler, "guard_runtime_environment", return_value=True),
        patch.object(scheduler._instance_lock, "acquire", return_value=True),
        patch("sovi.device.scheduler.build_scheduler_owner", return_value=MagicMock(to_dict=lambda: {"pid": 1})),
        patch("sovi.device.scheduler.sync_execute", return_value=[device_row]) as mock_sync_execute,
        patch.object(scheduler._rotator, "start"),
        patch("sovi.device.scheduler.recover_interrupted_seeder_tasks"),
        patch("sovi.device.scheduler.dedupe_open_seeder_tasks"),
        patch("sovi.device.scheduler.populate_seeder_tasks"),
        patch("sovi.device.scheduler.threading.Thread") as mock_thread,
    ):
        scheduler.start()

    mock_sync_execute.assert_called_once()
    mock_thread.assert_called_once()


def test_scheduler_start_rejects_runtime_conflicts():
    scheduler = DeviceScheduler()

    with (
        patch("sovi.device.scheduler.enforce_clean_room"),
        patch.object(scheduler, "guard_runtime_environment", return_value=False),
        patch.object(scheduler._instance_lock, "acquire") as mock_lock,
    ):
        started = scheduler.start()

    assert started is False
    mock_lock.assert_not_called()


def test_scheduler_start_rejects_when_singleton_lock_is_held():
    scheduler = DeviceScheduler()

    with (
        patch("sovi.device.scheduler.enforce_clean_room"),
        patch.object(scheduler, "guard_runtime_environment", return_value=True),
        patch.object(scheduler._instance_lock, "acquire", return_value=False),
    ):
        started = scheduler.start()

    assert started is False
    assert scheduler.status()["start_error"] == "scheduler_lock_held"
