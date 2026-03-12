"""Central configuration loaded from environment variables and YAML files."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sovi.models import VideoTier

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
NICHES_DIR = CONFIG_DIR / "niches"


def _resolve_env_file() -> str | None:
    """Return .env path only if it exists and is readable (not git-crypt encrypted)."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return None
    try:
        with open(env_path, "rb") as f:
            header = f.read(10)
        if header.startswith(b"\x00GITCRYPT"):
            logger.debug(".env is git-crypt encrypted — skipping, using env vars only")
            return None
        return str(env_path)
    except OSError:
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql://sovi:sovi@localhost:5432/sovi"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # AI Models
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    fal_key: str = ""
    openai_api_key: str = ""

    # Voice / Audio
    elevenlabs_api_key: str = ""
    deepgram_api_key: str = ""

    # Distribution
    late_api_key: str = ""

    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""

    # Proxies
    anyip_api_key: str = ""

    # CAPTCHA
    capsolver_api_key: str = ""
    twocaptcha_api_key: str = ""

    # SMS
    textverified_api_key: str = ""

    # Encryption
    sovi_master_key: str = ""

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "sovi"

    # gRPC Device Daemon
    device_daemon_host: str = "localhost:50051"

    # Production defaults
    default_video_tier: VideoTier = VideoTier.LOW_MID
    max_concurrent_productions: int = 10
    daily_video_target: int = 10

    # Identity guard
    identity_guard_enabled: bool = True
    min_cooldown_seconds: int = 300   # 5 min
    max_cooldown_seconds: int = 900   # 15 min
    max_sessions_per_device_day: int = 24

    # Paths
    output_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "output")
    temp_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "temp")


def load_niche_config(slug: str) -> dict[str, Any]:
    """Load a niche YAML config by slug name."""
    path = NICHES_DIR / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Niche config not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def load_all_niche_configs() -> dict[str, dict[str, Any]]:
    """Load all niche configs from the niches directory."""
    configs: dict[str, dict[str, Any]] = {}
    if not NICHES_DIR.exists():
        return configs
    for path in sorted(NICHES_DIR.glob("*.yaml")):
        slug = path.stem
        with open(path) as f:
            configs[slug] = yaml.safe_load(f)
    return configs


settings = Settings()
