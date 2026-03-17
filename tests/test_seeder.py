from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from sovi.device.account_creator import AccountCreationFailure

sys.modules.setdefault(
    "sovi.device.seeder_email",
    types.SimpleNamespace(create_protonmail_email=MagicMock()),
)

from sovi.device.seeder import _execute_account_creation


class TestExecuteAccountCreation:
    def test_raises_detailed_failure_when_account_creation_returns_none(self):
        wda = MagicMock()
        wda.toggle_airplane_mode.return_value = True
        wda.ensure_cellular_only.return_value = True
        task = {
            "persona_id": "persona-1",
            "niche_id": "niche-1",
            "first_name": "Jamie",
            "last_name": "Rodriguez",
            "display_name": "Jamie Rodriguez",
            "username_base": "jamie.rodriguez",
            "gender": "female",
            "date_of_birth": "1997-01-01",
            "age": 29,
            "bio_short": "bio",
            "occupation": "writer",
            "interests": ["travel"],
            "platform": "instagram",
        }
        failure = AccountCreationFailure(
            platform="instagram",
            step="verification",
            reason="Instagram requested a confirmation code but no code was retrieved",
            context={"email": "jamie@example.com"},
        )

        with (
            patch("sovi.persona.account_creator.create_account_for_persona", return_value=None),
            patch("sovi.device.account_creator.consume_last_account_creation_failure", return_value=failure),
            patch("sovi.device.seeder.events.emit") as mock_emit,
        ):
            with pytest.raises(RuntimeError, match="confirmation code"):
                _execute_account_creation(wda, task, "dev-1", "iPhone-B")

        assert any(call.args[2] == "persona_account_creation_failed" for call in mock_emit.call_args_list)

    def test_returns_none_when_post_rotation_cellular_verification_fails(self):
        wda = MagicMock()
        wda.toggle_airplane_mode.return_value = True
        wda.ensure_cellular_only.return_value = False
        task = {
            "persona_id": "persona-1",
            "niche_id": "niche-1",
            "first_name": "Jamie",
            "last_name": "Rodriguez",
            "display_name": "Jamie Rodriguez",
            "username_base": "jamie.rodriguez",
            "gender": "female",
            "date_of_birth": "1997-01-01",
            "age": 29,
            "bio_short": "bio",
            "occupation": "writer",
            "interests": ["travel"],
            "platform": "instagram",
        }

        with patch("sovi.device.seeder.events.emit") as mock_emit:
            result = _execute_account_creation(wda, task, "dev-1", "iPhone-B")

        assert result is None
        assert any(call.args[2] == "cellular_rotation_failed" for call in mock_emit.call_args_list)
