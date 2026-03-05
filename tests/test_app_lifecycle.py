"""Tests for app_lifecycle — login dispatch, delete, install, IDFA reset."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovi.device.app_lifecycle import (
    APP_NAMES,
    BUNDLES,
    delete_app,
    install_from_app_store,
    login_account,
    login_instagram,
    login_tiktok,
    reset_idfa,
)
from sovi.device.wda_client import BUNDLE_IDS


# --- Constants ---


class TestConstants:
    def test_bundles_is_bundle_ids(self):
        assert BUNDLES is BUNDLE_IDS

    def test_app_names_match_platforms(self):
        assert APP_NAMES["tiktok"] == "TikTok"
        assert APP_NAMES["instagram"] == "Instagram"


# --- login_account dispatch ---


class TestLoginAccount:
    def _make_wda(self):
        wda = MagicMock()
        wda.device = MagicMock()
        wda.device.name = "test"
        return wda

    def test_dispatch_tiktok(self):
        wda = self._make_wda()
        account = {
            "platform": "tiktok",
            "email": "t@test.com",
            "password": "pw123",
            "id": "acc-1",
        }
        with patch("sovi.device.app_lifecycle.login_tiktok", return_value=True) as mock_login:
            result = login_account(wda, account, device_id="dev-1")
        assert result is True
        mock_login.assert_called_once_with(
            wda, "t@test.com", "pw123", None,
            device_id="dev-1", account_id="acc-1",
        )

    def test_dispatch_instagram(self):
        wda = self._make_wda()
        account = {
            "platform": "instagram",
            "email": "i@test.com",
            "password": "pw456",
            "id": "acc-2",
        }
        with patch("sovi.device.app_lifecycle.login_instagram", return_value=True) as mock_login:
            result = login_account(wda, account, device_id="dev-1")
        assert result is True
        mock_login.assert_called_once_with(
            wda, "i@test.com", "pw456",
            device_id="dev-1", account_id="acc-2",
        )

    def test_dispatch_unsupported(self):
        wda = self._make_wda()
        account = {"platform": "snapchat", "email": "x", "password": "y", "id": "a"}
        result = login_account(wda, account, device_id="dev-1")
        assert result is False

    def test_decrypts_encrypted_credentials(self):
        wda = self._make_wda()
        account = {
            "platform": "tiktok",
            "email_enc": "enc_email",
            "password_enc": "enc_pw",
            "totp_secret_enc": "enc_totp",
            "id": "acc-3",
        }
        with (
            patch("sovi.device.app_lifecycle.decrypt", side_effect=["e@t.com", "p123", "TOTP"]),
            patch("sovi.device.app_lifecycle.login_tiktok", return_value=True) as mock_login,
        ):
            login_account(wda, account, device_id="dev-1")
        mock_login.assert_called_once_with(
            wda, "e@t.com", "p123", "TOTP",
            device_id="dev-1", account_id="acc-3",
        )

    def test_plaintext_email_preferred_over_enc(self):
        wda = self._make_wda()
        account = {
            "platform": "tiktok",
            "email": "plain@test.com",
            "email_enc": "should_not_decrypt",
            "password": "plainpw",
            "password_enc": "should_not_decrypt",
            "id": "acc-4",
        }
        with patch("sovi.device.app_lifecycle.login_tiktok", return_value=True) as mock_login:
            login_account(wda, account, device_id="dev-1")
        # Should use plaintext, not decrypt
        mock_login.assert_called_once_with(
            wda, "plain@test.com", "plainpw", None,
            device_id="dev-1", account_id="acc-4",
        )


# --- delete_app ---


class TestDeleteApp:
    def _make_wda(self):
        wda = MagicMock()
        wda._s = "http://localhost:8100"
        wda.client = MagicMock()
        return wda

    def test_unknown_platform(self):
        wda = self._make_wda()
        assert delete_app(wda, "snapchat") is False

    def test_wda_uninstall_success(self):
        wda = self._make_wda()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        wda.client.post.return_value = mock_resp

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = delete_app(wda, "tiktok", device_id="dev-1")
        assert result is True
        wda.terminate_app.assert_called_once_with(BUNDLE_IDS["tiktok"])

    def test_wda_uninstall_fallback_to_springboard(self):
        wda = self._make_wda()
        # First post (uninstall) fails, subsequent posts (touchAndHold) succeed
        uninstall_call = [True]  # flag to track first call
        def post_side_effect(*args, **kwargs):
            if uninstall_call[0]:
                uninstall_call[0] = False
                raise Exception("endpoint not available")
            return MagicMock(status_code=200)
        wda.client.post.side_effect = post_side_effect
        # Springboard: find app icon, then menu items
        wda.find_element.side_effect = [
            {"ELEMENT": "icon-1"},     # app icon found
            {"ELEMENT": "remove-el"},  # "Remove App" found
            None,                       # "Delete App" confirm not found
            {"ELEMENT": "del-el"},     # "Delete" found
        ]

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = delete_app(wda, "tiktok", device_id="dev-1")
        assert result is True

    def test_exception_returns_false(self):
        wda = self._make_wda()
        wda.terminate_app.side_effect = Exception("device offline")

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = delete_app(wda, "tiktok", device_id="dev-1")
        assert result is False


# --- reset_idfa ---


class TestResetIdfa:
    def _make_wda(self):
        wda = MagicMock()
        return wda

    def test_success_path(self):
        wda = self._make_wda()
        # find_element returns elements for Privacy, Tracking, Switch
        wda.find_element.side_effect = [
            {"ELEMENT": "privacy-el"},
            {"ELEMENT": "tracking-el"},
            {"ELEMENT": "switch-el"},
        ]

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = reset_idfa(wda, device_id="dev-1")
        assert result is True
        wda.launch_app.assert_called_once_with("com.apple.Preferences")
        wda.press_button.assert_called_with("home")

    def test_privacy_not_found(self):
        wda = self._make_wda()
        wda.find_element.return_value = None  # Never finds Privacy

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = reset_idfa(wda, device_id="dev-1")
        assert result is False

    def test_exception_returns_false_and_goes_home(self):
        wda = self._make_wda()
        wda.launch_app.side_effect = Exception("device offline")

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = reset_idfa(wda, device_id="dev-1")
        assert result is False
        wda.press_button.assert_called_with("home")


# --- install_from_app_store ---


class TestInstallFromAppStore:
    def _make_wda(self):
        wda = MagicMock()
        wda.device = MagicMock()
        wda.device.name = "test"
        return wda

    def test_unknown_platform(self):
        wda = self._make_wda()
        assert install_from_app_store(wda, "snapchat") is False

    def test_install_success(self):
        wda = self._make_wda()
        # find_element sequence: search tab, search field, search btn, GET btn
        wda.find_element.side_effect = [
            {"ELEMENT": "search-tab"},
            {"ELEMENT": "search-field"},
            {"ELEMENT": "search-btn"},
            {"ELEMENT": "get-btn"},
        ]
        wda.app_state.return_value = 1  # installed

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 1]),  # before deadline
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "tiktok", device_id="dev-1", timeout=120)
        assert result is True

    def test_install_timeout(self):
        wda = self._make_wda()
        wda.find_element.side_effect = [
            {"ELEMENT": "st"}, {"ELEMENT": "sf"}, {"ELEMENT": "sb"}, {"ELEMENT": "gb"},
        ]
        wda.app_state.return_value = 0  # not installed

        auto_mock = MagicMock()
        counter = [0]

        def fake_time():
            counter[0] += 1
            return counter[0] * 100  # jumps past deadline

        with (
            patch("time.sleep"),
            patch("time.time", side_effect=fake_time),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "tiktok", device_id="dev-1", timeout=10)
        assert result is False
