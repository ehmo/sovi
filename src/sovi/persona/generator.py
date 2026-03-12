"""Persona generation via LLM API (Gemini or Claude).

Generates diverse fictional personas for a niche, including name, demographics,
occupation, bio, and interests. Uses the niche's voice profile and content
pillars for context.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import date

from sovi.config import load_niche_config, settings

logger = logging.getLogger(__name__)

# US states weighted by population for realistic distribution
US_STATES = [
    ("California", ["Los Angeles", "San Francisco", "San Diego", "Sacramento"]),
    ("Texas", ["Houston", "Dallas", "Austin", "San Antonio"]),
    ("Florida", ["Miami", "Orlando", "Tampa", "Jacksonville"]),
    ("New York", ["New York City", "Brooklyn", "Queens", "Buffalo"]),
    ("Illinois", ["Chicago", "Springfield", "Naperville"]),
    ("Pennsylvania", ["Philadelphia", "Pittsburgh", "Allentown"]),
    ("Ohio", ["Columbus", "Cleveland", "Cincinnati"]),
    ("Georgia", ["Atlanta", "Savannah", "Augusta"]),
    ("North Carolina", ["Charlotte", "Raleigh", "Durham"]),
    ("Michigan", ["Detroit", "Grand Rapids", "Ann Arbor"]),
    ("New Jersey", ["Newark", "Jersey City", "Princeton"]),
    ("Virginia", ["Richmond", "Virginia Beach", "Arlington"]),
    ("Washington", ["Seattle", "Tacoma", "Spokane"]),
    ("Arizona", ["Phoenix", "Tucson", "Scottsdale"]),
    ("Massachusetts", ["Boston", "Cambridge", "Worcester"]),
    ("Tennessee", ["Nashville", "Memphis", "Knoxville"]),
    ("Indiana", ["Indianapolis", "Fort Wayne", "Bloomington"]),
    ("Missouri", ["Kansas City", "St. Louis", "Springfield"]),
    ("Maryland", ["Baltimore", "Bethesda", "Annapolis"]),
    ("Colorado", ["Denver", "Boulder", "Colorado Springs"]),
]


def _build_prompt(niche_config: dict, niche_slug: str, count: int) -> str:
    """Build the persona generation prompt."""
    voice = niche_config.get("voice_profile", {})
    pillars = niche_config.get("content_pillars", [])

    return f"""Generate {count} diverse fictional social media personas for the "{niche_config.get('name', niche_slug)}" niche.

Niche context:
- Voice: {voice.get('tone', 'authentic and relatable')}
- Content pillars: {', '.join(pillars) if isinstance(pillars, list) else str(pillars)}

Requirements:
- Mix of genders (roughly 50/50 male/female, with 1-2 nonbinary if count >= 8)
- Ages between 22 and 55, with variety
- Diverse ethnicities (names should reflect: White, Black, Hispanic, Asian, Middle Eastern, etc.)
- Each persona needs a plausible occupation related or adjacent to the niche
- Bio should be 1-2 sentences, authentic and engaging (like a real social media bio)
- Interests should be 4-6 items, mix of niche-related and personal hobbies
- Username base should be natural (like "emily.johnson.93" or "marcustech" or "sarahfinance")

Return a JSON array where each object has:
{{
  "first_name": "string",
  "last_name": "string",
  "display_name": "First Last",
  "gender": "female|male|nonbinary",
  "age": number,
  "occupation": "string",
  "state": "US state name",
  "city": "city name",
  "bio_short": "1-2 sentence social media bio",
  "bio_long": "3-4 sentence professional bio for LinkedIn",
  "interests": ["interest1", "interest2", ...],
  "personality": {{
    "tone": "casual|professional|energetic|calm|humorous",
    "emoji_usage": "none|light|moderate|heavy",
    "formality": "low|medium|high"
  }}
}}

Return ONLY the JSON array, no other text."""


def _call_gemini(prompt: str) -> str:
    """Call Google Gemini API and return text response."""
    import httpx

    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={settings.gemini_api_key}",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.9,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(prompt: str) -> str:
    """Call Anthropic Claude API and return text response."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _extract_json(text: str) -> list[dict]:
    """Parse JSON from LLM response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


def generate_personas(niche_id: str, niche_slug: str, count: int = 10) -> list[dict]:
    """Generate `count` personas for a niche using Gemini (preferred) or Claude.

    Produces diverse mix of ages (22-55), genders, ethnicities, occupations.
    Each persona gets: name, DOB, location, occupation, bio, interests, username.
    """
    # Prefer Gemini, fall back to Anthropic
    if not settings.gemini_api_key and not settings.anthropic_api_key:
        logger.error("No LLM API key configured (GEMINI_API_KEY or ANTHROPIC_API_KEY)")
        return []

    # Load niche config for context
    try:
        niche_config = load_niche_config(niche_slug)
    except FileNotFoundError:
        niche_config = {"name": niche_slug}

    prompt = _build_prompt(niche_config, niche_slug, count)

    try:
        if settings.gemini_api_key:
            logger.info("Using Gemini for persona generation")
            text = _call_gemini(prompt)
        else:
            logger.info("Using Claude for persona generation")
            text = _call_anthropic(prompt)

        raw_personas = _extract_json(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse persona JSON from LLM response")
        return []
    except Exception:
        logger.error("LLM API call failed for persona generation", exc_info=True)
        return []

    # Post-process: add derived fields
    personas = []
    for p in raw_personas:
        try:
            age = p["age"]
            # Generate a plausible DOB
            today = date.today()
            birth_year = today.year - age
            dob = date(birth_year, random.randint(1, 12), random.randint(1, 28))

            # Generate username base
            first = p["first_name"].lower().replace(" ", "")
            last = p["last_name"].lower().replace(" ", "")
            birth_suffix = str(dob.year)[-2:]
            username_variants = [
                f"{first}.{last}.{birth_suffix}",
                f"{first}{last}{birth_suffix}",
                f"{first}_{last}{birth_suffix}",
                f"{first}.{last}",
            ]
            username_base = random.choice(username_variants)

            persona = {
                "first_name": p["first_name"],
                "last_name": p["last_name"],
                "display_name": p.get("display_name", f"{p['first_name']} {p['last_name']}"),
                "username_base": username_base,
                "gender": p.get("gender", "female"),
                "date_of_birth": dob.isoformat(),
                "age": age,
                "country": "US",
                "state": p.get("state"),
                "city": p.get("city"),
                "occupation": p.get("occupation"),
                "bio_short": p["bio_short"],
                "bio_long": p.get("bio_long"),
                "interests": p.get("interests", []),
                "personality": json.dumps(p.get("personality", {})),
            }
            personas.append(persona)
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed persona: %s", e)
            continue

    logger.info("Generated %d personas for niche %s", len(personas), niche_slug)
    return personas
