"""Tests for hooks extractor — store_hook_template and extract_hook_template."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from sovi.hooks.extractor import EXTRACTION_PROMPT, extract_hook_template, store_hook_template


# --- store_hook_template ---


class TestStoreHookTemplate:
    async def test_stores_hook_and_template_separately(self):
        """F-093 regression: hook_text and template_text must be stored as separate columns."""
        template = {
            "template_text": "You won't believe [TOPIC] can do this",
            "category": "curiosity_gap",
            "emotional_tone": "surprise",
            "variables": ["TOPIC"],
            "quality_score": 0.85,
        }
        original_hook = "You won't believe ChatGPT can do this"

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            hook_id = await store_hook_template(
                template,
                niche_id=None,
                platform="tiktok",
                hook_text=original_hook,
            )

        assert isinstance(hook_id, UUID)
        # Verify the SQL params: hook_text and template_text are separate
        call_args = mock_exec.call_args
        params = call_args[0][1]
        # params order: (id, hook_text, template_text, category, emotional_tone, platform, niche_id, ...)
        assert params[1] == original_hook  # hook_text
        assert params[2] == "You won't believe [TOPIC] can do this"  # template_text
        assert params[1] != params[2]  # They must be different

    async def test_hook_text_falls_back_to_template_field(self):
        """When hook_text param is None, falls back to template.get('hook_text', '')."""
        template = {
            "template_text": "[NUMBER] reasons to [ACTION]",
            "hook_text": "5 reasons to invest",
            "category": "list_structure",
        }

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            await store_hook_template(template, platform="instagram")

        params = mock_exec.call_args[0][1]
        assert params[1] == "5 reasons to invest"  # from template dict
        assert params[2] == "[NUMBER] reasons to [ACTION]"  # template_text

    async def test_hook_text_defaults_to_empty_string(self):
        """When neither hook_text param nor template['hook_text'] exists, defaults to ''."""
        template = {
            "template_text": "This changed my life",
            "category": "personal_story",
        }

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            await store_hook_template(template)

        params = mock_exec.call_args[0][1]
        assert params[1] == ""  # hook_text defaults to empty string

    async def test_stores_category_and_tone(self):
        template = {
            "template_text": "I made $[NUMBER] in [TIMEFRAME]",
            "category": "proof_results",
            "emotional_tone": "excitement",
        }

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            await store_hook_template(template, platform="tiktok")

        params = mock_exec.call_args[0][1]
        assert params[3] == "proof_results"  # category
        assert params[4] == "excitement"  # emotional_tone

    async def test_niche_id_converted_to_string(self):
        template = {
            "template_text": "Test",
            "category": "bold_claim",
        }
        niche_uuid = UUID("12345678-1234-1234-1234-123456789012")

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            await store_hook_template(template, niche_id=niche_uuid)

        params = mock_exec.call_args[0][1]
        assert params[6] == str(niche_uuid)

    async def test_niche_id_none_passes_none(self):
        template = {
            "template_text": "Test",
            "category": "bold_claim",
        }

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            await store_hook_template(template, niche_id=None)

        params = mock_exec.call_args[0][1]
        assert params[6] is None

    async def test_sql_contains_thompson_defaults(self):
        """Thompson sampling priors should be initialized to 1.0."""
        template = {"template_text": "T", "category": "bold_claim"}

        with patch("sovi.hooks.extractor.db.execute", new_callable=AsyncMock) as mock_exec:
            await store_hook_template(template)

        query = mock_exec.call_args[0][0]
        assert "thompson_alpha" in query
        assert "thompson_beta" in query
        assert "1.0" in query


# --- extract_hook_template ---


class TestExtractHookTemplate:
    async def test_calls_anthropic_api(self):
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"template_text": "[TOPIC] hack", "category": "curiosity_gap", "emotional_tone": "surprise", "variables": ["TOPIC"], "quality_score": 0.9}')
        ]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("sovi.hooks.extractor.anthropic.AsyncAnthropic", return_value=mock_client):
            result = await extract_hook_template(
                hook_text="This ChatGPT hack changed everything",
                platform="tiktok",
                views=1_000_000,
                engagement_rate=5.2,
            )

        assert result["template_text"] == "[TOPIC] hack"
        assert result["category"] == "curiosity_gap"

    async def test_prompt_contains_content_details(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"template_text": "T", "category": "c"}')]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("sovi.hooks.extractor.anthropic.AsyncAnthropic", return_value=mock_client):
            await extract_hook_template(
                hook_text="Buy now before it's too late",
                platform="instagram",
                views=500_000,
                engagement_rate=3.1,
            )

        # Check the prompt was formatted correctly
        call_args = mock_client.messages.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
        prompt_text = messages[0]["content"]
        assert "instagram" in prompt_text
        assert "500000" in prompt_text
        assert "3.1" in prompt_text
        assert "Buy now before it's too late" in prompt_text


# --- EXTRACTION_PROMPT ---


class TestExtractionPrompt:
    def test_prompt_has_required_placeholders(self):
        assert "{platform}" in EXTRACTION_PROMPT
        assert "{views}" in EXTRACTION_PROMPT
        assert "{engagement_rate}" in EXTRACTION_PROMPT
        assert "{hook_text}" in EXTRACTION_PROMPT

    def test_prompt_specifies_json_format(self):
        assert "template_text" in EXTRACTION_PROMPT
        assert "category" in EXTRACTION_PROMPT
        assert "emotional_tone" in EXTRACTION_PROMPT
        assert "quality_score" in EXTRACTION_PROMPT
