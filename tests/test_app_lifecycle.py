"""Tests for app_lifecycle — login dispatch, delete, install, IDFA reset."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovi.device.app_lifecycle import (
    APP_NAMES,
    APP_STORE_URLS,
    BUNDLES,
    delete_app,
    install_from_app_store,
    login_account,
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
        assert wda.press_button.call_count >= 3

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
        assert wda.press_button.call_count >= 4

    def test_wda_uninstall_fallback_requires_delete_confirmation(self):
        wda = self._make_wda()
        uninstall_call = [True]

        def post_side_effect(*args, **kwargs):
            if uninstall_call[0]:
                uninstall_call[0] = False
                raise Exception("endpoint not available")
            return MagicMock(status_code=200)

        wda.client.post.side_effect = post_side_effect
        wda.find_element.side_effect = [
            {"ELEMENT": "icon-1"},     # app icon found
            {"ELEMENT": "remove-el"},  # "Remove App" found
            None,                       # "Delete App" confirm not found
            None,                       # "Delete" confirm not found
        ]

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = delete_app(wda, "tiktok", device_id="dev-1")
        assert result is False
        assert wda.press_button.call_count >= 4

    def test_exception_returns_false(self):
        wda = self._make_wda()
        wda.terminate_app.side_effect = Exception("device offline")

        with patch("time.sleep"), patch("sovi.device.app_lifecycle.events.emit"):
            result = delete_app(wda, "tiktok", device_id="dev-1")
        assert result is False
        assert wda.press_button.call_count >= 2


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
        wda.find_element.side_effect = lambda using, value: (
            {"ELEMENT": "get-btn"} if using == "accessibility id" and value == "GET" else None
        )
        wda.app_state.side_effect = [0, 1]  # pre-install: not installed, then installed

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 1]),  # before deadline
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "tiktok", device_id="dev-1", timeout=120)
        assert result is True
        wda.open_url.assert_called_once_with(APP_STORE_URLS["tiktok"])

    def test_install_succeeds_when_offer_button_reports_open(self):
        wda = self._make_wda()

        def find_element_side_effect(using, value):
            if using == "predicate string" and 'AppStore.offerButton' in value and "open" in value:
                return {"ELEMENT": "offer-open"}
            return None

        wda.find_element.side_effect = find_element_side_effect
        wda.app_state.return_value = 1

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "instagram", device_id="dev-1", timeout=30)

        assert result is True
        wda.element_click.assert_not_called()
        wda.press_button.assert_called_with("home")

    def test_install_starts_when_offer_button_reports_get(self):
        wda = self._make_wda()

        def find_element_side_effect(using, value):
            if using == "predicate string" and 'AppStore.offerButton' in value and "get" in value:
                return {"ELEMENT": "offer-get"}
            return None

        wda.find_element.side_effect = find_element_side_effect
        wda.app_state.side_effect = [0, 1]

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 1]),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "instagram", device_id="dev-1", timeout=30)

        assert result is True
        wda.element_click.assert_called_once_with("offer-get")

    def test_install_succeeds_when_app_state_is_stale_but_offer_button_is_open(self):
        wda = self._make_wda()
        calls = {"count": 0}

        def find_element_side_effect(using, value):
            if using == "accessibility id" and value == "GET" and calls["count"] == 0:
                calls["count"] += 1
                return {"ELEMENT": "get-btn"}
            if using == "predicate string" and 'AppStore.offerButton' in value and "open" in value:
                return {"ELEMENT": "offer-open"}
            return None

        wda.find_element.side_effect = find_element_side_effect
        wda.app_state.side_effect = [1, 1]

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 1]),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "instagram", device_id="dev-1", timeout=30)

        assert result is True
        wda.element_click.assert_called_once_with("get-btn")
        wda.press_button.assert_called_with("home")

    def test_install_retries_after_session_churn_on_product_page(self):
        wda = self._make_wda()
        rounds = {"value": 0}

        def find_element_side_effect(using, value):
            if rounds["value"] == 0:
                return None
            if using == "predicate string" and 'AppStore.offerButton' in value and "open" in value:
                return {"ELEMENT": "offer-open"}
            return None

        def reconnect_side_effect():
            rounds["value"] += 1

        wda.find_element.side_effect = find_element_side_effect
        wda.connect.side_effect = reconnect_side_effect
        wda.app_state.return_value = 1

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "instagram", device_id="dev-1", timeout=30)

        assert result is True
        assert wda.connect.called
        assert wda.open_url.call_count == 2

    def test_install_reconnects_when_polling_hits_invalid_session(self):
        wda = self._make_wda()
        calls = {"count": 0}

        def find_element_side_effect(using, value):
            if using == "predicate string" and 'AppStore.offerButton' in value and "get" in value and calls["count"] == 0:
                calls["count"] += 1
                return {"ELEMENT": "offer-get"}
            if using == "predicate string" and 'AppStore.offerButton' in value and "open" in value and calls["count"] >= 1:
                return {"ELEMENT": "offer-open"}
            return None

        wda.find_element.side_effect = find_element_side_effect
        wda.app_state.side_effect = [0, RuntimeError("invalid session id"), 1]

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 1]),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "instagram", device_id="dev-1", timeout=30)

        assert result is True
        assert wda.connect.called
        assert wda.open_url.call_count >= 2

    def test_install_falls_back_to_search_when_product_page_lookup_fails(self):
        wda = self._make_wda()

        def find_element_side_effect(using, value):
            if using == "accessibility id" and value == "Search":
                return {"ELEMENT": "search-tab"}
            if using == "class chain" and value == "**/XCUIElementTypeSearchField":
                return {"ELEMENT": "search-field"}
            if using == "accessibility id" and value == "search":
                return {"ELEMENT": "submit-search"}
            if (
                using == "predicate string"
                and 'AppStore.offerButton' in value
                and "get" in value
                and wda.launch_app.called
            ):
                return {"ELEMENT": "offer-get"}
            return None

        wda.find_element.side_effect = find_element_side_effect
        wda.app_state.side_effect = [0, 1]

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("time.time", side_effect=[0, 0, 1]),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "instagram", device_id="dev-1", timeout=30)

        assert result is True
        assert wda.launch_app.called

    def test_install_timeout(self):
        wda = self._make_wda()
        wda.find_element.side_effect = lambda using, value: (
            {"ELEMENT": "get-btn"} if using == "accessibility id" and value == "GET" else None
        )
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

    def test_install_fails_when_button_not_found(self):
        wda = self._make_wda()
        wda.find_element.return_value = None
        wda.app_state.return_value = 0

        auto_mock = MagicMock()
        with (
            patch("time.sleep"),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = install_from_app_store(wda, "tiktok", device_id="dev-1", timeout=10)
        assert result is False


class TestLoginTikTok:
    def _make_wda(self):
        wda = MagicMock()
        wda.device = MagicMock()
        wda.device.name = "test"
        return wda

    def test_missing_email_field_returns_false(self):
        wda = self._make_wda()
        auto_mock = MagicMock()

        def find_element_side_effect(using, value):
            if using == "accessibility id" and value in {"Log in", "Login"}:
                return {"ELEMENT": "login-btn"}
            if using == "predicate string" and "SecureTextField" in value:
                return {"ELEMENT": "password-field"}
            return None

        wda.find_element.side_effect = find_element_side_effect

        with (
            patch("time.sleep"),
            patch("random.uniform", return_value=0.1),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
        ):
            result = login_tiktok(wda, "t@test.com", "pw", device_id="dev-1", account_id="acc-1")
        assert result is False

    def test_verification_failure_returns_false(self):
        wda = self._make_wda()
        auto_mock = MagicMock()

        def find_element_side_effect(using, value):
            if using == "accessibility id" and value in {"Log in", "Login"}:
                return {"ELEMENT": "login-btn"}
            if (
                using == "predicate string"
                and "TextField" in value
                and "SecureTextField" not in value
            ):
                return {"ELEMENT": "email-field"}
            if using == "predicate string" and "SecureTextField" in value:
                return {"ELEMENT": "password-field"}
            return None

        wda.find_element.side_effect = find_element_side_effect

        with (
            patch("time.sleep"),
            patch("random.uniform", return_value=0.1),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
            patch("sovi.device.app_lifecycle._confirm_tiktok_login", return_value=False),
        ):
            result = login_tiktok(wda, "t@test.com", "pw", device_id="dev-1", account_id="acc-1")
        assert result is False

    def test_success_requires_verification(self):
        wda = self._make_wda()
        auto_mock = MagicMock()

        def find_element_side_effect(using, value):
            if using == "accessibility id" and value in {"Log in", "Login"}:
                return {"ELEMENT": "login-btn"}
            if (
                using == "predicate string"
                and "TextField" in value
                and "SecureTextField" not in value
            ):
                return {"ELEMENT": "email-field"}
            if using == "predicate string" and "SecureTextField" in value:
                return {"ELEMENT": "password-field"}
            return None

        wda.find_element.side_effect = find_element_side_effect

        with (
            patch("time.sleep"),
            patch("random.uniform", return_value=0.1),
            patch("sovi.device.app_lifecycle.events.emit"),
            patch("sovi.device.app_lifecycle.DeviceAutomation", return_value=auto_mock),
            patch("sovi.device.app_lifecycle._confirm_tiktok_login", return_value=True),
        ):
            result = login_tiktok(wda, "t@test.com", "pw", device_id="dev-1", account_id="acc-1")
        assert result is True
        wda.element_value.assert_any_call("email-field", "t@test.com")
        wda.element_value.assert_any_call("password-field", "pw")
