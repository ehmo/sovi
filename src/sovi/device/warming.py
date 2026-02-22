"""Account warming automation for TikTok and Instagram.

Uses direct WDA client (no Appium middleware) for reliability.

Warming phases:
- PASSIVE (Days 1-3): Pure consumption, zero interactions
- LIGHT (Days 4-7): Light engagement (likes, follows)
- MODERATE (Days 8-14): First posts + moderate engagement
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum

from sovi.device.wda_client import WDASession, DeviceAutomation

logger = logging.getLogger(__name__)


class WarmingPhase(IntEnum):
    PASSIVE = 1
    LIGHT = 2
    MODERATE = 3
    ACTIVE = 4


# ---------- TikTok Warming ----------


class TikTokWarmer:
    """TikTok account warming."""

    BUNDLE = "com.zhiliaoapp.musically"

    def __init__(self, wda: WDASession, auto: DeviceAutomation) -> None:
        self.wda = wda
        self.auto = auto

    def _open(self) -> None:
        self.wda.launch_app(self.BUNDLE)
        time.sleep(random.uniform(3.0, 5.0))
        # Light popup dismiss — only check alerts, skip heavy element search
        self._dismiss_alerts()

    def _dismiss_alerts(self) -> None:
        """Lightweight alert dismissal — no element search (TikTok UI is too heavy)."""
        alert_text = self.wda.get_alert_text()
        if alert_text:
            logger.info("TikTok alert: %s", str(alert_text)[:80])
            self.wda.dismiss_alert()
            time.sleep(0.5)

    def passive_consumption(self, duration_min: int = 30) -> dict:
        """Phase 1: Watch FYP content passively. Zero interactions."""
        logger.info("TikTok passive consumption for %d min", duration_min)
        self._open()

        videos_watched = 0
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            # Watch current video for variable duration
            if random.random() < 0.3:
                watch_time = random.uniform(20, 60)  # Watch to completion
            else:
                watch_time = random.uniform(5, 25)  # Partial watch

            time.sleep(watch_time)
            videos_watched += 1

            # Lightweight alert check every ~5 videos (avoid heavy element search)
            if videos_watched % 5 == 0:
                self._dismiss_alerts()

            # Swipe to next
            self.wda.swipe_up(duration=random.uniform(0.3, 0.6))
            time.sleep(random.uniform(0.5, 1.5))

            # Occasional pause (simulate reading comments or zoning out)
            if random.random() < 0.08:
                time.sleep(random.uniform(5, 15))

        elapsed = (time.time() - start) / 60
        logger.info("Passive: %d videos in %.1f min", videos_watched, elapsed)
        return {"phase": "passive", "videos_watched": videos_watched, "duration_min": elapsed}

    def light_engagement(self, duration_min: int = 20) -> dict:
        """Phase 2: Light engagement — likes, follows."""
        logger.info("TikTok light engagement for %d min", duration_min)
        self._open()

        likes = 0
        follows = 0
        videos_watched = 0
        max_likes = random.randint(5, 10)
        max_follows = random.randint(3, 7)
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            # Watch video
            watch_time = random.uniform(8, 40)
            time.sleep(watch_time)
            videos_watched += 1

            if videos_watched % 5 == 0:
                self._dismiss_alerts()

            # Like (double-tap center) with rate limit
            if likes < max_likes and random.random() < 0.15:
                self.auto.like_current()
                likes += 1
                logger.debug("Liked video #%d", videos_watched)
                time.sleep(random.uniform(30, 90))  # Min gap between likes

            # Follow (tap + button on profile)
            if follows < max_follows and random.random() < 0.06:
                if self.auto.tap_element("accessibility id", "Follow"):
                    follows += 1
                    logger.debug("Followed creator")
                    time.sleep(random.uniform(30, 60))

            # Next video
            self.wda.swipe_up(duration=random.uniform(0.3, 0.6))
            time.sleep(random.uniform(0.5, 2.0))

        elapsed = (time.time() - start) / 60
        logger.info("Light: %d videos, %d likes, %d follows in %.1f min", videos_watched, likes, follows, elapsed)
        return {
            "phase": "light_engagement",
            "videos_watched": videos_watched,
            "likes": likes,
            "follows": follows,
            "duration_min": elapsed,
        }

    def search_niche_hashtags(self, hashtags: list[str]) -> dict:
        """Search niche hashtags to train the algorithm."""
        logger.info("Searching niche hashtags: %s", hashtags[:3])
        self._open()
        searched = 0

        # Try to tap Search/Discover
        if not self.auto.tap_element("accessibility id", "Search"):
            if not self.auto.tap_element("accessibility id", "Discover"):
                logger.warning("Could not find search button")
                return {"searched": 0}

        time.sleep(random.uniform(2, 4))

        for tag in hashtags[: random.randint(2, 4)]:
            try:
                # Find search field
                search_el = self.wda.find_element("class chain", "**/XCUIElementTypeSearchField")
                if not search_el:
                    continue

                el_id = search_el.get("ELEMENT", "")
                if not el_id:
                    continue

                self.wda.element_click(el_id)
                time.sleep(0.5)

                # Type hashtag
                self.wda.element_value(el_id, f"#{tag}")
                time.sleep(random.uniform(1.5, 3.0))

                # Press search
                self.wda.press_button("home")  # Dismiss keyboard
                time.sleep(0.5)

                # Browse results
                browse_time = random.uniform(30, 90)
                browse_end = time.time() + browse_time
                while time.time() < browse_end:
                    time.sleep(random.uniform(5, 12))
                    self.wda.swipe_up(duration=random.uniform(0.4, 0.7))

                searched += 1
                time.sleep(random.uniform(2, 5))

            except Exception:
                logger.debug("Error searching %s", tag, exc_info=True)

        return {"searched": searched}


# ---------- Instagram Warming ----------


class InstagramWarmer:
    """Instagram account warming."""

    BUNDLE = "com.burbn.instagram"

    def __init__(self, wda: WDASession, auto: DeviceAutomation) -> None:
        self.wda = wda
        self.auto = auto

    def _open(self) -> None:
        self.wda.launch_app(self.BUNDLE)
        time.sleep(random.uniform(2.0, 4.0))
        self._dismiss_alerts()

    def _dismiss_alerts(self) -> None:
        """Lightweight alert dismissal — no element search."""
        alert_text = self.wda.get_alert_text()
        if alert_text:
            logger.info("Instagram alert: %s", str(alert_text)[:80])
            self.wda.dismiss_alert()
            time.sleep(0.5)

    def passive_consumption(self, duration_min: int = 20) -> dict:
        """Phase 1: Browse feed and Reels passively."""
        logger.info("Instagram passive consumption for %d min", duration_min)
        self._open()

        posts_viewed = 0
        reels_watched = 0
        start = time.time()
        end_time = start + duration_min * 60
        feed_end = start + (duration_min * 60 * 0.4)  # 40% feed, 60% reels

        # Feed browsing
        while time.time() < feed_end:
            time.sleep(random.uniform(3, 10))
            self.wda.swipe_up(duration=random.uniform(0.5, 0.9))
            posts_viewed += 1

            if posts_viewed % 5 == 0:
                self._dismiss_alerts()

        # Switch to Reels — use direct WDA to avoid heavy element search
        reels_el = self.wda.find_element("accessibility id", "Reels")
        if reels_el:
            el_id = reels_el.get("ELEMENT", "")
            if el_id:
                self.wda.element_click(el_id)
        time.sleep(random.uniform(2, 4))

        # Reels watching
        while time.time() < end_time:
            if random.random() < 0.25:
                time.sleep(random.uniform(20, 60))
            else:
                time.sleep(random.uniform(5, 25))
            reels_watched += 1

            if reels_watched % 5 == 0:
                self._dismiss_alerts()

            self.wda.swipe_up(duration=random.uniform(0.3, 0.6))
            time.sleep(random.uniform(0.5, 1.5))

        elapsed = (time.time() - start) / 60
        logger.info("Passive: %d posts, %d reels in %.1f min", posts_viewed, reels_watched, elapsed)
        return {
            "phase": "passive",
            "posts_viewed": posts_viewed,
            "reels_watched": reels_watched,
            "duration_min": elapsed,
        }

    def light_engagement(self, duration_min: int = 20) -> dict:
        """Phase 2: Light engagement — likes, follows."""
        logger.info("Instagram light engagement for %d min", duration_min)
        self._open()

        likes = 0
        follows = 0
        max_likes = random.randint(5, 10)
        max_follows = random.randint(3, 5)
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            time.sleep(random.uniform(5, 15))

            if likes % 5 == 0:
                self._dismiss_alerts()

            if likes < max_likes and random.random() < 0.12:
                self.auto.like_current()
                likes += 1
                time.sleep(random.uniform(30, 90))

            if follows < max_follows and random.random() < 0.06:
                follow_el = self.wda.find_element(
                    "predicate string",
                    'label == "Follow" AND type == "XCUIElementTypeButton"',
                )
                if follow_el:
                    el_id = follow_el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                        follows += 1
                        time.sleep(random.uniform(30, 60))

            self.wda.swipe_up(duration=random.uniform(0.5, 0.8))
            time.sleep(random.uniform(1, 3))

        elapsed = (time.time() - start) / 60
        logger.info("Light: %d likes, %d follows in %.1f min", likes, follows, elapsed)
        return {"phase": "light_engagement", "likes": likes, "follows": follows, "duration_min": elapsed}


# ---------- Reddit Warming ----------


class RedditWarmer:
    """Reddit account warming via on-device browsing."""

    BUNDLE = "com.reddit.Reddit"

    def __init__(self, wda: WDASession, auto: DeviceAutomation) -> None:
        self.wda = wda
        self.auto = auto

    def _open(self) -> None:
        self.wda.launch_app(self.BUNDLE)
        time.sleep(random.uniform(2.0, 4.0))
        self._dismiss_alerts()

    def _dismiss_alerts(self) -> None:
        """Lightweight alert dismissal — no element search."""
        alert_text = self.wda.get_alert_text()
        if alert_text:
            logger.info("Reddit alert: %s", str(alert_text)[:80])
            self.wda.dismiss_alert()
            time.sleep(0.5)

    def passive_consumption(self, duration_min: int = 20) -> dict:
        """Browse home feed, read posts, scroll through content."""
        logger.info("Reddit passive consumption for %d min", duration_min)
        self._open()

        posts_viewed = 0
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            # Read current post (variable time based on content)
            read_time = random.uniform(3, 15)
            time.sleep(read_time)
            posts_viewed += 1

            if posts_viewed % 8 == 0:
                self._dismiss_alerts()

            # Scroll to next post
            self.wda.swipe_up(duration=random.uniform(0.4, 0.8))
            time.sleep(random.uniform(0.5, 2.0))

            # Occasionally tap into a post and read comments
            if random.random() < 0.15:
                # Tap center to open post
                size = self.wda.screen_size()
                self.wda.tap(size["width"] // 2, int(size["height"] * 0.4))
                time.sleep(random.uniform(3, 12))
                # Scroll through comments
                for _ in range(random.randint(1, 4)):
                    self.wda.swipe_up(duration=random.uniform(0.4, 0.7))
                    time.sleep(random.uniform(2, 5))
                # Go back (swipe right or tap back)
                self.wda.swipe(
                    0, size["height"] // 2,
                    size["width"], size["height"] // 2,
                    duration=0.3,
                )
                time.sleep(random.uniform(1, 3))

            # Occasional longer pause (reading a long post)
            if random.random() < 0.05:
                time.sleep(random.uniform(10, 30))

        elapsed = (time.time() - start) / 60
        logger.info("Reddit passive: %d posts in %.1f min", posts_viewed, elapsed)
        return {"phase": "passive", "posts_viewed": posts_viewed, "duration_min": elapsed}

    def light_engagement(self, duration_min: int = 20) -> dict:
        """Light engagement: upvotes, occasional comments."""
        logger.info("Reddit light engagement for %d min", duration_min)
        self._open()

        upvotes = 0
        max_upvotes = random.randint(5, 15)
        posts_viewed = 0
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            time.sleep(random.uniform(3, 12))
            posts_viewed += 1

            if posts_viewed % 8 == 0:
                self._dismiss_alerts()

            # Upvote (tap the upvote button)
            if upvotes < max_upvotes and random.random() < 0.12:
                upvote_el = self.wda.find_element(
                    "predicate string",
                    'name CONTAINS "upvote" OR name CONTAINS "Upvote"',
                )
                if upvote_el:
                    el_id = upvote_el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                        upvotes += 1
                        logger.debug("Upvoted post #%d", posts_viewed)
                        time.sleep(random.uniform(15, 45))

            # Scroll
            self.wda.swipe_up(duration=random.uniform(0.4, 0.8))
            time.sleep(random.uniform(0.5, 2.0))

        elapsed = (time.time() - start) / 60
        logger.info("Reddit light: %d posts, %d upvotes in %.1f min", posts_viewed, upvotes, elapsed)
        return {
            "phase": "light_engagement",
            "posts_viewed": posts_viewed,
            "upvotes": upvotes,
            "duration_min": elapsed,
        }


# ---------- YouTube Warming ----------


class YouTubeWarmer:
    """YouTube account warming — Shorts + Home feed."""

    BUNDLE = "com.google.ios.youtube"

    def __init__(self, wda: WDASession, auto: DeviceAutomation) -> None:
        self.wda = wda
        self.auto = auto

    def _open(self) -> None:
        self.wda.launch_app(self.BUNDLE)
        time.sleep(random.uniform(3.0, 5.0))
        self._dismiss_alerts()

    def _dismiss_alerts(self) -> None:
        alert_text = self.wda.get_alert_text()
        if alert_text:
            logger.info("YouTube alert: %s", str(alert_text)[:80])
            self.wda.dismiss_alert()
            time.sleep(0.5)

    def passive_consumption(self, duration_min: int = 30) -> dict:
        """Browse Home feed + watch Shorts passively."""
        logger.info("YouTube passive consumption for %d min", duration_min)
        self._open()

        videos_watched = 0
        shorts_watched = 0
        start = time.time()
        end_time = start + duration_min * 60
        shorts_start = start + (duration_min * 60 * 0.4)  # 40% home, 60% Shorts

        # Home feed browsing
        while time.time() < shorts_start:
            time.sleep(random.uniform(5, 20))
            self.wda.swipe_up(duration=random.uniform(0.5, 0.9))
            videos_watched += 1

            if videos_watched % 5 == 0:
                self._dismiss_alerts()

            # Occasionally tap a video thumbnail and watch
            if random.random() < 0.15:
                size = self.wda.screen_size()
                self.wda.tap(size["width"] // 2, int(size["height"] * 0.35))
                time.sleep(random.uniform(15, 60))
                # Navigate back
                back_el = self.wda.find_element("accessibility id", "Collapse")
                if back_el:
                    el_id = back_el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                time.sleep(random.uniform(1, 3))

        # Navigate to Shorts tab
        shorts_el = self.wda.find_element("accessibility id", "Shorts")
        if shorts_el:
            el_id = shorts_el.get("ELEMENT", "")
            if el_id:
                self.wda.element_click(el_id)
        time.sleep(random.uniform(2, 4))

        # Shorts watching (swipe-up like TikTok)
        while time.time() < end_time:
            if random.random() < 0.3:
                watch_time = random.uniform(20, 58)  # Watch full Short
            else:
                watch_time = random.uniform(5, 20)
            time.sleep(watch_time)
            shorts_watched += 1

            if shorts_watched % 5 == 0:
                self._dismiss_alerts()

            self.wda.swipe_up(duration=random.uniform(0.3, 0.6))
            time.sleep(random.uniform(0.5, 1.5))

        elapsed = (time.time() - start) / 60
        logger.info("YouTube passive: %d home, %d shorts in %.1f min",
                     videos_watched, shorts_watched, elapsed)
        return {
            "phase": "passive",
            "videos_watched": videos_watched,
            "shorts_watched": shorts_watched,
            "duration_min": elapsed,
        }

    def light_engagement(self, duration_min: int = 20) -> dict:
        """Phase 2: Light engagement — likes on Shorts."""
        logger.info("YouTube light engagement for %d min", duration_min)
        self._open()

        likes = 0
        shorts_watched = 0
        max_likes = random.randint(3, 8)
        start = time.time()
        end_time = start + duration_min * 60

        # Go to Shorts
        shorts_el = self.wda.find_element("accessibility id", "Shorts")
        if shorts_el:
            el_id = shorts_el.get("ELEMENT", "")
            if el_id:
                self.wda.element_click(el_id)
        time.sleep(random.uniform(2, 4))

        while time.time() < end_time:
            watch_time = random.uniform(8, 40)
            time.sleep(watch_time)
            shorts_watched += 1

            if shorts_watched % 5 == 0:
                self._dismiss_alerts()

            # Like (tap like button on right side of Shorts)
            if likes < max_likes and random.random() < 0.12:
                like_el = self.wda.find_element("accessibility id", "Like")
                if like_el:
                    el_id = like_el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                        likes += 1
                        time.sleep(random.uniform(20, 60))

            self.wda.swipe_up(duration=random.uniform(0.3, 0.6))
            time.sleep(random.uniform(0.5, 2.0))

        elapsed = (time.time() - start) / 60
        logger.info("YouTube light: %d shorts, %d likes in %.1f min",
                     shorts_watched, likes, elapsed)
        return {
            "phase": "light_engagement",
            "shorts_watched": shorts_watched,
            "likes": likes,
            "duration_min": elapsed,
        }


# ---------- X/Twitter Warming ----------


class XTwitterWarmer:
    """X/Twitter account warming — timeline browsing."""

    BUNDLE = "com.atebits.Tweetie2"

    def __init__(self, wda: WDASession, auto: DeviceAutomation) -> None:
        self.wda = wda
        self.auto = auto

    def _open(self) -> None:
        self.wda.launch_app(self.BUNDLE)
        time.sleep(random.uniform(2.0, 4.0))
        self._dismiss_alerts()

    def _dismiss_alerts(self) -> None:
        alert_text = self.wda.get_alert_text()
        if alert_text:
            logger.info("X/Twitter alert: %s", str(alert_text)[:80])
            self.wda.dismiss_alert()
            time.sleep(0.5)

    def passive_consumption(self, duration_min: int = 20) -> dict:
        """Browse timeline, read tweets/threads, watch embedded videos."""
        logger.info("X/Twitter passive consumption for %d min", duration_min)
        self._open()

        tweets_viewed = 0
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            # Read current tweet(s) on screen
            read_time = random.uniform(2, 10)
            time.sleep(read_time)
            tweets_viewed += 1

            if tweets_viewed % 8 == 0:
                self._dismiss_alerts()

            # Scroll to next tweets
            self.wda.swipe_up(duration=random.uniform(0.4, 0.8))
            time.sleep(random.uniform(0.5, 2.0))

            # Occasionally tap into a tweet to read replies
            if random.random() < 0.10:
                size = self.wda.screen_size()
                self.wda.tap(size["width"] // 2, int(size["height"] * 0.35))
                time.sleep(random.uniform(3, 15))
                # Scroll replies
                for _ in range(random.randint(1, 3)):
                    self.wda.swipe_up(duration=random.uniform(0.4, 0.7))
                    time.sleep(random.uniform(2, 5))
                # Back
                back_el = self.wda.find_element("accessibility id", "Back")
                if back_el:
                    el_id = back_el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                time.sleep(random.uniform(1, 2))

            # Occasional longer pause
            if random.random() < 0.05:
                time.sleep(random.uniform(8, 20))

        elapsed = (time.time() - start) / 60
        logger.info("X/Twitter passive: %d tweets in %.1f min", tweets_viewed, elapsed)
        return {"phase": "passive", "tweets_viewed": tweets_viewed, "duration_min": elapsed}

    def light_engagement(self, duration_min: int = 20) -> dict:
        """Phase 2: Light engagement — likes and occasional replies."""
        logger.info("X/Twitter light engagement for %d min", duration_min)
        self._open()

        likes = 0
        tweets_viewed = 0
        max_likes = random.randint(5, 12)
        start = time.time()
        end_time = start + duration_min * 60

        while time.time() < end_time:
            time.sleep(random.uniform(3, 10))
            tweets_viewed += 1

            if tweets_viewed % 8 == 0:
                self._dismiss_alerts()

            # Like (heart button)
            if likes < max_likes and random.random() < 0.10:
                like_el = self.wda.find_element(
                    "predicate string",
                    'name CONTAINS "Like" AND type == "XCUIElementTypeButton"',
                )
                if like_el:
                    el_id = like_el.get("ELEMENT", "")
                    if el_id:
                        self.wda.element_click(el_id)
                        likes += 1
                        time.sleep(random.uniform(20, 60))

            self.wda.swipe_up(duration=random.uniform(0.4, 0.8))
            time.sleep(random.uniform(0.5, 2.0))

        elapsed = (time.time() - start) / 60
        logger.info("X/Twitter light: %d tweets, %d likes in %.1f min",
                     tweets_viewed, likes, elapsed)
        return {
            "phase": "light_engagement",
            "tweets_viewed": tweets_viewed,
            "likes": likes,
            "duration_min": elapsed,
        }


# ---------- Session orchestrator ----------


@dataclass
class WarmingConfig:
    device_name: str
    platform: str
    phase: WarmingPhase
    niche_hashtags: list[str] = field(default_factory=list)
    duration_min: int = 30


def run_warming(wda: WDASession, config: WarmingConfig) -> dict:
    """Execute a warming session."""
    auto = DeviceAutomation(wda)

    logger.info("Warming: %s %s phase=%d on %s", config.platform, config.device_name, config.phase, wda.device.name)

    if config.platform == "tiktok":
        warmer = TikTokWarmer(wda, auto)
        if config.phase == WarmingPhase.PASSIVE:
            return warmer.passive_consumption(config.duration_min)
        else:
            return warmer.light_engagement(config.duration_min)

    elif config.platform == "instagram":
        warmer = InstagramWarmer(wda, auto)
        if config.phase == WarmingPhase.PASSIVE:
            return warmer.passive_consumption(config.duration_min)
        else:
            return warmer.light_engagement(config.duration_min)

    elif config.platform == "reddit":
        warmer = RedditWarmer(wda, auto)
        if config.phase == WarmingPhase.PASSIVE:
            return warmer.passive_consumption(config.duration_min)
        else:
            return warmer.light_engagement(config.duration_min)

    elif config.platform == "youtube":
        warmer = YouTubeWarmer(wda, auto)
        if config.phase == WarmingPhase.PASSIVE:
            return warmer.passive_consumption(config.duration_min)
        else:
            return warmer.light_engagement(config.duration_min)

    elif config.platform in ("twitter", "x_twitter"):
        warmer = XTwitterWarmer(wda, auto)
        if config.phase == WarmingPhase.PASSIVE:
            return warmer.passive_consumption(config.duration_min)
        else:
            return warmer.light_engagement(config.duration_min)

    else:
        return {"error": f"Unsupported platform: {config.platform}"}
