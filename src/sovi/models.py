"""Pydantic models for data flowing through the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


# === Enums matching DB schema ===


class Platform(StrEnum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube_shorts"
    REDDIT = "reddit"
    TWITTER = "x_twitter"


class ContentFormat(StrEnum):
    FACELESS = "faceless"
    REDDIT_STORY = "reddit_story"
    AI_AVATAR = "ai_avatar"
    CAROUSEL = "carousel"
    MEME = "meme"
    LISTICLE = "listicle"


class VideoTier(StrEnum):
    FREE = "free"
    BUDGET = "budget"
    LOW_MID = "low_mid"
    MID = "mid"
    PREMIUM = "premium"
    CINEMATIC = "cinematic"


class AccountState(StrEnum):
    CREATED = "created"
    WARMING_P1 = "warming_p1"
    WARMING_P2 = "warming_p2"
    WARMING_P3 = "warming_p3"
    ACTIVE = "active"
    RESTING = "resting"
    COOLDOWN = "cooldown"
    FLAGGED = "flagged"
    RESTRICTED = "restricted"
    SHADOWBANNED = "shadowbanned"
    SUSPENDED = "suspended"
    BANNED = "banned"


class ProductionStatus(StrEnum):
    SCRIPTING = "scripting"
    GENERATING = "generating"
    ASSEMBLING = "assembling"
    DISTRIBUTING = "distributing"
    COMPLETE = "complete"
    FAILED = "failed"


class HookCategory(StrEnum):
    CURIOSITY_GAP = "curiosity_gap"
    BOLD_CLAIM = "bold_claim"
    PROBLEM_PAIN = "problem_pain"
    PROOF_RESULTS = "proof_results"
    NUMBERS_DATA = "numbers_data"
    URGENCY_FOMO = "urgency_fomo"
    LIST_STRUCTURE = "list_structure"
    PERSONAL_STORY = "personal_story"
    SHOCK_TENSION = "shock_tension"
    DIRECT_CALLOUT = "direct_callout"


# === Pipeline Data Models ===


class TopicCandidate(BaseModel):
    """A trending topic identified by the research engine."""

    topic: str
    niche_slug: str
    platform: Platform
    source_url: str | None = None
    trend_score: float = 0.0
    overperformance_ratio: float | None = None


class ScriptRequest(BaseModel):
    """Input to the script generator."""

    topic: TopicCandidate
    content_format: ContentFormat
    hook_template_id: UUID | None = None
    target_duration_s: int = 45
    target_platforms: list[Platform] = Field(default_factory=lambda: [Platform.TIKTOK])


class GeneratedScript(BaseModel):
    """Output from the script generator."""

    script_id: UUID
    hook_text: str
    body_text: str
    cta_text: str
    full_text: str
    word_count: int
    estimated_duration_s: float
    hook_category: HookCategory
    hook_template_id: UUID | None = None


class AssetSpec(BaseModel):
    """Specification for a single generated asset."""

    asset_type: str  # voiceover, image, video_clip, music, background
    prompt: str | None = None
    source_url: str | None = None
    tier: VideoTier = VideoTier.LOW_MID
    duration_s: float | None = None


class GeneratedAsset(BaseModel):
    """A produced asset ready for assembly."""

    asset_type: str
    file_path: str
    duration_s: float | None = None
    cost_usd: float = 0.0
    model_used: str | None = None


class PlatformExport(BaseModel):
    """Export spec for a target platform."""

    platform: Platform
    file_path: str
    width: int = 1080
    height: int = 1920
    max_file_size_mb: float = 500.0
    codec: str = "h264"
    audio_codec: str = "aac"
    caption_text: str = ""
    hashtags: list[str] = Field(default_factory=list)


class QualityReport(BaseModel):
    """Result of automated quality checks."""

    passed: bool
    score: float  # 0.0 - 1.0
    resolution_ok: bool = True
    bitrate_ok: bool = True
    audio_ok: bool = True
    caption_accuracy: float = 1.0
    safe_zone_ok: bool = True
    content_policy_ok: bool = True
    blocking_failures: list[str] = Field(default_factory=list)


class DistributionRequest(BaseModel):
    """Request to post content to a platform."""

    content_id: UUID
    account_id: UUID
    platform: Platform
    export_path: str
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    scheduled_at: datetime | None = None


class EngagementSnapshot(BaseModel):
    """Point-in-time engagement metrics for a posted piece of content."""

    distribution_id: UUID
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    completion_rate: float | None = None
    engagement_rate: float | None = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
