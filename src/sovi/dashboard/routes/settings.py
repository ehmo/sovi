"""Settings page — API key status and connection testing."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from sovi.config import settings
from sovi.dashboard.app import templates

router = APIRouter(tags=["settings"])

# API keys to check (env var name → display label)
API_KEYS = {
    "anthropic_api_key": "Anthropic (Claude)",
    "fal_key": "fal.ai (images/video)",
    "openai_api_key": "OpenAI TTS",
    "elevenlabs_api_key": "ElevenLabs (voice)",
    "deepgram_api_key": "Deepgram (transcription)",
    "capsolver_api_key": "CapSolver (CAPTCHA)",
    "textverified_api_key": "TextVerified (SMS)",
    "sovi_master_key": "Master encryption key",
    "reddit_client_id": "Reddit API",
    "anyip_api_key": "AnyIP (proxies)",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    keys = _key_status()
    return templates.TemplateResponse("settings.html", {"request": request, "keys": keys})


@router.get("/api/settings/keys")
async def api_key_status():
    """Which API keys are configured (returns booleans, never the values)."""
    return _key_status()


@router.get("/api/settings/test/{service}")
async def test_connection(service: str):
    """Test connectivity to a service."""
    if service == "db":
        return await _test_db()
    elif service == "captcha":
        return _test_key("capsolver_api_key", "CapSolver")
    elif service == "sms":
        return _test_key("textverified_api_key", "TextVerified")
    elif service == "imap":
        return {"ok": False, "message": "IMAP test not implemented — configure per-account"}
    else:
        return {"ok": False, "message": f"Unknown service: {service}"}


def _key_status() -> dict[str, dict]:
    result = {}
    for attr, label in API_KEYS.items():
        value = getattr(settings, attr, "")
        result[attr] = {
            "label": label,
            "configured": bool(value and value.strip()),
        }
    return result


def _test_key(attr: str, label: str) -> dict:
    value = getattr(settings, attr, "")
    if value and value.strip():
        return {"ok": True, "message": f"{label} key is configured"}
    return {"ok": False, "message": f"{label} key is not configured"}


async def _test_db() -> dict:
    try:
        from sovi.db import execute_one
        row = await execute_one("SELECT 1 AS ok")
        if row and row["ok"] == 1:
            return {"ok": True, "message": "Database connected"}
        return {"ok": False, "message": "Database query failed"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
