"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from sovi.config import (
    CONFIG_DIR,
    NICHES_DIR,
    Platform,
    Settings,
    VideoTier,
    load_all_niche_configs,
    load_niche_config,
)


def test_settings_defaults():
    s = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
    )
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.temporal_host == "localhost:7233"
    assert s.default_video_tier == VideoTier.LOW_MID
    assert s.daily_video_target == 10


def test_platform_enum():
    assert Platform.TIKTOK == "tiktok"
    assert Platform.REDDIT == "reddit"
    assert len(Platform) == 5


def test_video_tier_enum():
    assert VideoTier.FREE == "free"
    assert VideoTier.CINEMATIC == "cinematic"
    assert len(VideoTier) == 6


def test_niches_dir_exists():
    assert NICHES_DIR.exists(), f"Niches directory not found at {NICHES_DIR}"


def test_load_personal_finance_niche():
    config = load_niche_config("personal_finance")
    assert config["slug"] == "personal_finance"
    assert config["tier"] == 1
    assert config["active"] is True
    assert "budgeting_basics" in config["content_pillars"]
    assert "tiktok" in config["platforms"]
    assert "reddit" in config["platforms"]


def test_load_all_niches():
    configs = load_all_niche_configs()
    assert len(configs) >= 3
    assert "personal_finance" in configs
    assert "ai_storytelling" in configs
    assert "tech_ai_tools" in configs


def test_load_missing_niche():
    with pytest.raises(FileNotFoundError):
        load_niche_config("nonexistent_niche_xyz")
