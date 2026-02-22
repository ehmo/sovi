"""Tests for Pydantic data models."""

from __future__ import annotations

from uuid import uuid4

from sovi.models import (
    AccountState,
    ContentFormat,
    EngagementSnapshot,
    HookCategory,
    Platform,
    ProductionStatus,
    QualityReport,
    ScriptRequest,
    TopicCandidate,
    VideoTier,
)


def test_topic_candidate():
    t = TopicCandidate(
        topic="5 Morning Habits That Changed My Life",
        niche_slug="personal_finance",
        platform=Platform.TIKTOK,
        trend_score=85.0,
    )
    assert t.platform == "tiktok"
    assert t.source_url is None


def test_script_request():
    topic = TopicCandidate(
        topic="AI Tools Nobody Talks About",
        niche_slug="tech_ai_tools",
        platform=Platform.TIKTOK,
    )
    req = ScriptRequest(
        topic=topic,
        content_format=ContentFormat.FACELESS_NARRATION,
        target_duration_s=45,
    )
    assert req.target_platforms == [Platform.TIKTOK]


def test_quality_report_pass():
    qr = QualityReport(passed=True, score=0.85)
    assert qr.passed
    assert qr.blocking_failures == []


def test_quality_report_fail():
    qr = QualityReport(
        passed=False,
        score=0.3,
        resolution_ok=False,
        blocking_failures=["Resolution 720x1280 below 1080x1920"],
    )
    assert not qr.passed
    assert len(qr.blocking_failures) == 1


def test_engagement_snapshot():
    snap = EngagementSnapshot(
        distribution_id=uuid4(),
        views=10000,
        likes=500,
        comments=50,
        shares=100,
        saves=200,
        completion_rate=0.72,
    )
    assert snap.views == 10000
    assert snap.retention_3s is None


def test_all_enums_complete():
    assert len(Platform) == 5
    assert len(ContentFormat) == 6
    assert len(VideoTier) == 6
    assert len(AccountState) == 12
    assert len(ProductionStatus) == 7
    assert len(HookCategory) == 10
