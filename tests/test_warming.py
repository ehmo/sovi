"""Tests for warming module — BaseWarmer, platform warmers, run_warming dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovi.device.warming import (
    BaseWarmer,
    InstagramWarmer,
    PLATFORM_WARMERS,
    RedditWarmer,
    TikTokWarmer,
    WarmingConfig,
    WarmingPhase,
    XTwitterWarmer,
    YouTubeWarmer,
    run_warming,
)


def _make_mocks():
    """Create mock WDASession and DeviceAutomation."""
    wda = MagicMock()
    wda.device.name = "test-device"
    wda.get_alert_text.return_value = None
    wda.screen_size.return_value = {"width": 1080, "height": 1920}
    auto = MagicMock()
    return wda, auto


# --- BaseWarmer ---


class TestBaseWarmer:
    def test_init_stores_references(self):
        wda, auto = _make_mocks()
        warmer = TikTokWarmer(wda, auto)
        assert warmer.wda is wda
        assert warmer.auto is auto

    def test_dismiss_alerts_no_alert(self):
        wda, auto = _make_mocks()
        warmer = TikTokWarmer(wda, auto)
        warmer._dismiss_alerts()
        wda.get_alert_text.assert_called_once()
        wda.dismiss_alert.assert_not_called()

    def test_dismiss_alerts_with_alert(self):
        wda, auto = _make_mocks()
        wda.get_alert_text.return_value = "Allow notifications?"
        warmer = TikTokWarmer(wda, auto)
        with patch("time.sleep"):
            warmer._dismiss_alerts()
        wda.dismiss_alert.assert_called_once()

    def test_periodic_alert_check_fires_at_interval(self):
        wda, auto = _make_mocks()
        warmer = TikTokWarmer(wda, auto)
        # counter=5 with default interval=5 should fire
        warmer._periodic_alert_check(5)
        wda.get_alert_text.assert_called_once()

    def test_periodic_alert_check_skips_between_intervals(self):
        wda, auto = _make_mocks()
        warmer = TikTokWarmer(wda, auto)
        warmer._periodic_alert_check(3)
        wda.get_alert_text.assert_not_called()

    def test_periodic_alert_check_custom_interval(self):
        wda, auto = _make_mocks()
        warmer = RedditWarmer(wda, auto)
        # Reddit uses interval=8
        warmer._periodic_alert_check(8, interval=8)
        wda.get_alert_text.assert_called_once()

        wda.reset_mock()
        warmer._periodic_alert_check(7, interval=8)
        wda.get_alert_text.assert_not_called()

    def test_open_launches_app(self):
        wda, auto = _make_mocks()
        warmer = TikTokWarmer(wda, auto)
        with patch("time.sleep"):
            warmer._open()
        wda.launch_app.assert_called_once_with("com.zhiliaoapp.musically")

    def test_base_warmer_not_implemented(self):
        wda, auto = _make_mocks()
        warmer = BaseWarmer(wda, auto)
        try:
            warmer.passive_consumption()
            assert False, "Should raise NotImplementedError"
        except NotImplementedError:
            pass
        try:
            warmer.light_engagement()
            assert False, "Should raise NotImplementedError"
        except NotImplementedError:
            pass


# --- Platform warmer class attributes ---


class TestPlatformWarmerConfig:
    def test_tiktok_bundle(self):
        assert TikTokWarmer.BUNDLE == "com.zhiliaoapp.musically"
        assert TikTokWarmer.PLATFORM_NAME == "TikTok"
        assert TikTokWarmer.OPEN_DELAY == (3.0, 5.0)

    def test_instagram_bundle(self):
        assert InstagramWarmer.BUNDLE == "com.burbn.instagram"
        assert InstagramWarmer.PLATFORM_NAME == "Instagram"
        assert InstagramWarmer.OPEN_DELAY == (2.0, 4.0)  # default

    def test_reddit_bundle(self):
        assert RedditWarmer.BUNDLE == "com.reddit.Reddit"
        assert RedditWarmer.PLATFORM_NAME == "Reddit"

    def test_youtube_bundle(self):
        assert YouTubeWarmer.BUNDLE == "com.google.ios.youtube"
        assert YouTubeWarmer.PLATFORM_NAME == "YouTube"
        assert YouTubeWarmer.OPEN_DELAY == (3.0, 5.0)

    def test_xtwitter_bundle(self):
        assert XTwitterWarmer.BUNDLE == "com.atebits.Tweetie2"
        assert XTwitterWarmer.PLATFORM_NAME == "X/Twitter"

    def test_all_warmers_inherit_base(self):
        for cls in (TikTokWarmer, InstagramWarmer, RedditWarmer, YouTubeWarmer, XTwitterWarmer):
            assert issubclass(cls, BaseWarmer)

    def test_all_warmers_have_bundle(self):
        for cls in (TikTokWarmer, InstagramWarmer, RedditWarmer, YouTubeWarmer, XTwitterWarmer):
            assert cls.BUNDLE, f"{cls.__name__} has no BUNDLE"
            assert cls.PLATFORM_NAME, f"{cls.__name__} has no PLATFORM_NAME"


# --- PLATFORM_WARMERS registry ---


class TestPlatformWarmerRegistry:
    def test_all_platforms_registered(self):
        assert "tiktok" in PLATFORM_WARMERS
        assert "instagram" in PLATFORM_WARMERS
        assert "reddit" in PLATFORM_WARMERS
        assert "youtube" in PLATFORM_WARMERS
        assert "twitter" in PLATFORM_WARMERS
        assert "x_twitter" in PLATFORM_WARMERS

    def test_twitter_aliases_same_class(self):
        assert PLATFORM_WARMERS["twitter"] is PLATFORM_WARMERS["x_twitter"]
        assert PLATFORM_WARMERS["twitter"] is XTwitterWarmer

    def test_registry_maps_to_correct_classes(self):
        assert PLATFORM_WARMERS["tiktok"] is TikTokWarmer
        assert PLATFORM_WARMERS["instagram"] is InstagramWarmer
        assert PLATFORM_WARMERS["reddit"] is RedditWarmer
        assert PLATFORM_WARMERS["youtube"] is YouTubeWarmer


# --- run_warming dispatch ---


class TestRunWarming:
    def test_unsupported_platform_returns_error(self):
        wda, _ = _make_mocks()
        config = WarmingConfig(
            device_name="test",
            platform="snapchat",
            phase=WarmingPhase.PASSIVE,
        )
        result = run_warming(wda, config)
        assert "error" in result
        assert "snapchat" in result["error"]

    def test_passive_phase_dispatches_passive_consumption(self):
        wda, _ = _make_mocks()
        config = WarmingConfig(
            device_name="test",
            platform="tiktok",
            phase=WarmingPhase.PASSIVE,
            duration_min=1,
        )
        with patch.object(TikTokWarmer, "passive_consumption", return_value={"phase": "passive"}) as mock_pc:
            result = run_warming(wda, config)
            mock_pc.assert_called_once_with(1)
            assert result["phase"] == "passive"

    def test_light_phase_dispatches_light_engagement(self):
        wda, _ = _make_mocks()
        config = WarmingConfig(
            device_name="test",
            platform="instagram",
            phase=WarmingPhase.LIGHT,
            duration_min=5,
        )
        with patch.object(InstagramWarmer, "light_engagement", return_value={"phase": "light"}) as mock_le:
            result = run_warming(wda, config)
            mock_le.assert_called_once_with(5)
            assert result["phase"] == "light"

    def test_moderate_phase_dispatches_light_engagement(self):
        """MODERATE and ACTIVE phases also go through light_engagement."""
        wda, _ = _make_mocks()
        config = WarmingConfig(
            device_name="test",
            platform="reddit",
            phase=WarmingPhase.MODERATE,
            duration_min=10,
        )
        with patch.object(RedditWarmer, "light_engagement", return_value={"ok": True}) as mock_le:
            run_warming(wda, config)
            mock_le.assert_called_once_with(10)

    def test_twitter_alias_works(self):
        wda, _ = _make_mocks()
        for platform in ("twitter", "x_twitter"):
            config = WarmingConfig(
                device_name="test",
                platform=platform,
                phase=WarmingPhase.PASSIVE,
                duration_min=1,
            )
            with patch.object(XTwitterWarmer, "passive_consumption", return_value={"ok": True}):
                result = run_warming(wda, config)
                assert "error" not in result


# --- WarmingConfig ---


class TestWarmingConfig:
    def test_defaults(self):
        config = WarmingConfig(
            device_name="dev1",
            platform="tiktok",
            phase=WarmingPhase.PASSIVE,
        )
        assert config.duration_min == 30
        assert config.niche_hashtags == []

    def test_custom_values(self):
        config = WarmingConfig(
            device_name="dev1",
            platform="instagram",
            phase=WarmingPhase.LIGHT,
            niche_hashtags=["fitness", "gym"],
            duration_min=15,
        )
        assert config.duration_min == 15
        assert config.niche_hashtags == ["fitness", "gym"]


# --- WarmingPhase ---


class TestWarmingPhase:
    def test_ordering(self):
        assert WarmingPhase.PASSIVE < WarmingPhase.LIGHT
        assert WarmingPhase.LIGHT < WarmingPhase.MODERATE
        assert WarmingPhase.MODERATE < WarmingPhase.ACTIVE

    def test_values(self):
        assert WarmingPhase.PASSIVE == 1
        assert WarmingPhase.LIGHT == 2
        assert WarmingPhase.MODERATE == 3
        assert WarmingPhase.ACTIVE == 4
