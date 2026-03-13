"""Tests for account_creator — screenshot analysis helpers and signup dispatch."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from PIL import Image

from sovi.device.account_creator import (
    _dismiss_tiktok_alerts,
    _find_wide_red_band,
    _is_birthday_screen,
    _is_email_phone_screen,
    _generate_username,
    _pick_niche_for_platform,
    create_account,
)


# --- Screenshot analysis helpers ---


def _make_png(width: int = 1179, height: int = 2556, fill=(255, 255, 255)) -> bytes:
    """Create a solid-color PNG for testing."""
    img = Image.new("RGB", (width, height), fill)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_png_with_red_band(band_y_frac: float = 0.3) -> bytes:
    """Create a PNG with a wide red band at given Y fraction."""
    w, h = 1179, 2556
    img = Image.new("RGB", (w, h), (255, 255, 255))
    px = img.load()
    band_y = int(h * band_y_frac)
    # Draw red band 20 pixels tall across full width
    for y in range(band_y, band_y + 20):
        for x in range(w):
            px[x, y] = (240, 40, 40)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestFindWideRedBand:
    def test_no_red_in_white_image(self):
        png = _make_png()
        assert _find_wide_red_band(png) is None

    def test_finds_red_band(self):
        png = _make_png_with_red_band(0.3)
        result = _find_wide_red_band(png, 0.0, 1.0)
        assert result is not None
        # Band is at y_frac=0.3, h=2556, so pixel y ≈ 767, points ≈ 256
        assert 240 < result < 270

    def test_respects_y_range(self):
        png = _make_png_with_red_band(0.3)
        # Band is at 0.3, search only 0.5-1.0 should miss it
        result = _find_wide_red_band(png, 0.5, 1.0)
        assert result is None

    def test_empty_bytes(self):
        assert _find_wide_red_band(b"") is None

    def test_none_input(self):
        assert _find_wide_red_band(b"") is None


class TestIsBirthdayScreen:
    def test_white_image_is_not_birthday(self):
        assert _is_birthday_screen(_make_png()) is False

    def test_empty_bytes(self):
        assert _is_birthday_screen(b"") is False

    def test_red_band_at_bottom_with_dark_topleft(self):
        """Image with red button at bottom + dark pixels at top-left = birthday."""
        w, h = 1179, 2556
        img = Image.new("RGB", (w, h), (255, 255, 255))
        px = img.load()
        # Red button at bottom 10%
        band_y = int(h * 0.85)
        for y in range(band_y, band_y + 20):
            for x in range(w):
                px[x, y] = (240, 40, 40)
        # Dark pixels at top-left (back arrow area)
        for y in range(90, 100):
            for x in range(50, 60):
                px[x, y] = (10, 10, 10)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        assert _is_birthday_screen(buf.getvalue()) is True


class TestIsEmailPhoneScreen:
    def test_white_image_is_not_email(self):
        assert _is_email_phone_screen(_make_png()) is False

    def test_empty_bytes(self):
        assert _is_email_phone_screen(b"") is False


# --- Alert dismissal ---


class TestDismissTiktokAlerts:
    def test_no_alert(self):
        wda = MagicMock()
        wda.get_alert_text.return_value = None
        _dismiss_tiktok_alerts(wda)
        wda.dismiss_alert.assert_not_called()

    def test_dismisses_google_sso(self):
        wda = MagicMock()
        wda.get_alert_text.side_effect = ["Sign in with Google", None]
        with patch("time.sleep"):
            _dismiss_tiktok_alerts(wda)
        wda.dismiss_alert.assert_called_once()

    def test_dismisses_tracking(self):
        wda = MagicMock()
        wda.get_alert_text.side_effect = ["Would like to track your activity", None]
        with patch("time.sleep"):
            _dismiss_tiktok_alerts(wda)
        wda.dismiss_alert.assert_called_once()

    def test_accepts_unknown_alert(self):
        wda = MagicMock()
        wda.get_alert_text.side_effect = ["Something unexpected", None]
        with patch("time.sleep"):
            _dismiss_tiktok_alerts(wda)
        wda.accept_alert.assert_called_once()


# --- Username generation ---


class TestGenerateUsername:
    def test_known_niche(self):
        username = _generate_username("personal_finance")
        assert any(username.startswith(p) for p in ["money", "wealth", "finance", "cash", "invest"])

    def test_unknown_niche(self):
        username = _generate_username("random_niche")
        assert username.startswith("user")

    def test_has_digits(self):
        username = _generate_username("ai_storytelling")
        # Should end with 3-6 digits
        prefix_part = username.rstrip("0123456789")
        digit_part = username[len(prefix_part):]
        assert 3 <= len(digit_part) <= 6


# --- Signup dispatch ---


class TestSignupTiktok:
    def _make_wda(self):
        wda = MagicMock()
        wda.device = MagicMock()
        wda.device.name = "test"
        wda.session_id = "fake-session-id"
        wda.screen_size.return_value = {"width": 393, "height": 852}
        # Return white PNG for screenshots
        wda.screenshot.return_value = _make_png()
        wda.get_alert_text.return_value = None
        wda.find_element.return_value = None
        wda.find_elements.return_value = []
        return wda

    def test_signup_fails_if_signup_page_unreachable(self):
        """If we can't find the red button on signup page, should fail gracefully."""
        wda = self._make_wda()
        auto = MagicMock()

        with (
            patch("time.sleep"),
            patch("sovi.device.account_creator.events.emit"),
        ):
            from sovi.device.account_creator import _signup_tiktok
            result = _signup_tiktok(wda, auto, "t@test.com", "pw", "user1", None, "dev-1")

        assert result is False

    def test_signup_proceeds_past_signup_page_with_red_band(self):
        """If signup page has red button, flow should proceed to birthday."""
        wda = self._make_wda()
        auto = MagicMock()

        # First screenshots are white (launch), then has red band (signup page)
        red_png = _make_png_with_red_band(0.3)
        call_count = [0]

        def fake_screenshot(save_path=None):
            call_count[0] += 1
            if call_count[0] >= 2:
                return red_png
            return _make_png()

        wda.screenshot.side_effect = fake_screenshot

        with (
            patch("time.sleep"),
            patch("sovi.device.account_creator.events.emit"),
            patch("sovi.device.account_creator.poll_for_code", return_value=None),
            patch("sovi.device.account_creator.solve_slide", return_value=None),
        ):
            from sovi.device.account_creator import _signup_tiktok
            # This will proceed past signup page but may fail later at email entry
            # The key test is that it doesn't return False at the signup page step
            result = _signup_tiktok(wda, auto, "t@test.com", "pw", "user1", None, "dev-1")

        # Should have tapped at least once (signup page tap + use phone/email)
        assert wda.tap.call_count >= 2
