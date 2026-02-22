"""Automated hook extraction from viral content using Claude API."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import anthropic

from sovi import db
from sovi.config import settings

EXTRACTION_PROMPT = """Analyze this viral video's hook (first 3 seconds of the transcript/caption).

Content:
- Platform: {platform}
- Views: {views}
- Engagement rate: {engagement_rate}%
- First 3 seconds: "{hook_text}"

Tasks:
1. Extract the hook pattern
2. Abstract it into a reusable template by replacing specifics with [VARIABLES]
3. Categorize it

Respond in this exact JSON format:
{{
  "template_text": "The abstracted template with [TOPIC], [NUMBER], [AUDIENCE] variables",
  "category": "one of: curiosity_gap, bold_claim, problem_pain, proof_results, numbers_data, urgency_fomo, list_structure, personal_story, shock_tension, direct_callout",
  "emotional_tone": "primary emotion triggered",
  "variables": ["list", "of", "variable", "names"],
  "quality_score": 0.0 to 1.0
}}"""


async def extract_hook_template(
    hook_text: str,
    platform: str,
    views: int,
    engagement_rate: float,
) -> dict:
    """Extract a hook template from viral content using Claude."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    response = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": EXTRACTION_PROMPT.format(
                platform=platform,
                views=views,
                engagement_rate=engagement_rate,
                hook_text=hook_text,
            ),
        }],
    )

    return json.loads(response.content[0].text)


async def store_hook_template(
    template: dict,
    niche_id: UUID | None = None,
    platform: str | None = None,
) -> UUID:
    """Store an extracted hook template in the database."""
    hook_id = uuid4()
    await db.execute(
        """INSERT INTO hooks
           (id, hook_text, template_text, hook_category, emotional_tone,
            platform, niche_id, thompson_alpha, thompson_beta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 1.0, 1.0)""",
        (
            str(hook_id),
            template["template_text"],
            template["template_text"],
            template["category"],
            template.get("emotional_tone", ""),
            platform,
            str(niche_id) if niche_id else None,
        ),
    )
    return hook_id
