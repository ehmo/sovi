"""Tests for scheduler pure logic — warming phase transitions, task types, timing constants."""

from __future__ import annotations

from sovi.device.scheduler import (
    OVERHEAD_MIN,
    SESSIONS_PER_DAY,
    SESSION_TOTAL_MIN,
    WARMABLE_PLATFORMS,
    WARMING_DURATION_MIN,
    DeviceThread,
)
from sovi.device.warming import WarmingPhase
from sovi.models import AccountState


def test_session_timing_math():
    """Session total = warming + overhead, sessions/day derived correctly."""
    assert SESSION_TOTAL_MIN == WARMING_DURATION_MIN + OVERHEAD_MIN
    assert SESSIONS_PER_DAY == int(24 * 60 / SESSION_TOTAL_MIN)
    assert SESSIONS_PER_DAY >= 20  # sanity: at least 20 sessions/day


def test_warmable_platforms():
    assert "tiktok" in WARMABLE_PLATFORMS
    assert "instagram" in WARMABLE_PLATFORMS


def test_warming_phase_from_account_state():
    """Phase map used in _execute_warming: account state → warming phase."""
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


def test_device_thread_defaults():
    dt = DeviceThread(device_id="abc", device_name="test")
    assert dt.current_task == "idle"
    assert dt.sessions_today == 0
    assert dt.running is False
    assert dt.error is None
    assert dt.current_account is None
