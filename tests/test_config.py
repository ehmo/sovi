"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

from sovi.config import (
    CONFIG_DIR,
    NICHES_DIR,
    Settings,
    _resolve_env_file,
    load_all_niche_configs,
    load_niche_config,
)
from sovi.models import Platform, VideoTier


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
    assert len(Platform) == 7


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


# --- _resolve_env_file ---


class TestResolveEnvFile:
    def test_no_env_file(self, tmp_path):
        with patch("sovi.config.PROJECT_ROOT", tmp_path):
            result = _resolve_env_file()
        assert result is None

    def test_plain_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("DATABASE_URL=postgresql://x\n")
        with patch("sovi.config.PROJECT_ROOT", tmp_path):
            result = _resolve_env_file()
        assert result == str(env_path)

    def test_git_crypt_encrypted_env(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_bytes(b"\x00GITCRYPT\x00" + b"\xff" * 100)
        with patch("sovi.config.PROJECT_ROOT", tmp_path):
            result = _resolve_env_file()
        assert result is None

    def test_unreadable_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("x")
        with (
            patch("sovi.config.PROJECT_ROOT", tmp_path),
            patch("builtins.open", side_effect=OSError("permission denied")),
        ):
            result = _resolve_env_file()
        assert result is None
