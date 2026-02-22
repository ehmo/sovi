"""Script generation via Claude Sonnet API with hook template integration.

Uses hook templates from the database (selected via Thompson Sampling)
and niche voice profiles from YAML configs to generate platform-specific scripts.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

import anthropic

from sovi.config import load_niche_config, settings
from sovi.models import (
    ContentFormat,
    GeneratedScript,
    HookCategory,
    Platform,
    ScriptRequest,
    TopicCandidate,
)

logger = logging.getLogger(__name__)

SCRIPT_SYSTEM_PROMPT = """You are an expert short-form video scriptwriter for social media. You write scripts that:
- Hook viewers in the first 1-3 seconds using the provided hook template
- Follow the AIDA framework: Attention (0-3s) → Interest (3-15s) → Desire (15-40s) → Action (final 5s)
- Match the voice profile and tone specified for the niche
- Include platform-appropriate CTAs
- Stay within the target word count for the specified duration
- Sound natural and conversational, never robotic or over-produced
- Use short, punchy sentences that work well as voiceover

Output ONLY valid JSON in this format — no markdown, no backticks:
{
  "hook": "The opening hook (first 1-3 seconds, ~5-8 words)",
  "body": "The main content body. Use line breaks for natural pauses.",
  "cta": "The call-to-action ending (5-8 words)",
  "hook_category": "one of: curiosity_gap, bold_claim, problem_pain, proof_results, numbers_data, urgency_fomo, list_structure, personal_story, shock_tension, direct_callout"
}"""

# Platform-specific CTA strategies
PLATFORM_CTAS = {
    "tiktok": "Follow for more [TOPIC] content",
    "instagram": "Save this for later",
    "youtube_shorts": "Subscribe for more [TOPIC]",
    "reddit": "What do you think? Drop a comment",
    "x_twitter": "Repost if you agree",
}


async def generate_script(
    request: ScriptRequest,
    hook_template: dict | None = None,
) -> GeneratedScript:
    """Generate a video script using Claude Sonnet with optional hook template.

    Args:
        request: The script request with topic, format, duration, etc.
        hook_template: Optional hook template from the database (selected via Thompson Sampling).
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # ~2.5 words per second for natural speaking pace
    target_words = int(request.target_duration_s * 2.5)

    # Load niche voice profile
    voice_info = ""
    try:
        niche_cfg = load_niche_config(request.topic.niche_slug)
        voice = niche_cfg.get("voice_profile", {})
        if voice:
            voice_info = f"""
Voice profile for this niche:
- Tone: {voice.get('tone', 'conversational')}
- Style: {voice.get('style', 'educational')}
- Vocabulary level: {voice.get('vocabulary', 'accessible')}
- Target audience: {voice.get('audience', 'general')}"""
    except FileNotFoundError:
        pass

    # Hook template guidance
    hook_guidance = ""
    if hook_template:
        hook_guidance = f"""
Use this hook template as your opening (fill in the variables):
Template: "{hook_template.get('template_text', '')}"
Category: {hook_template.get('hook_category', 'curiosity_gap')}
Tone: {hook_template.get('emotional_tone', 'engaging')}"""

    # Platform-specific CTA
    primary_platform = request.target_platforms[0].value if request.target_platforms else "tiktok"
    cta_hint = PLATFORM_CTAS.get(primary_platform, "Follow for more").replace("[TOPIC]", request.topic.niche_slug.replace("_", " "))

    user_prompt = f"""Write a {request.target_duration_s}-second short-form video script.

Topic: {request.topic.topic}
Niche: {request.topic.niche_slug}
Format: {request.content_format.value}
Target platforms: {', '.join(p.value for p in request.target_platforms)}
Target word count: {target_words} words (~{request.target_duration_s} seconds at 2.5 words/sec)
{voice_info}
{hook_guidance}

Requirements:
- Hook MUST grab attention in the first 1-3 seconds ({int(target_words * 0.1)}-{int(target_words * 0.15)} words)
- Body delivers clear, specific value — no fluff, no filler
- End with a CTA like: "{cta_hint}"
- Tone: engaging, conversational, authentic
- No hashtags in the script (those go in captions separately)
- No "Hey guys" or "What's up" openers — go straight to the hook"""

    response = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=SCRIPT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text.strip()
    # Handle potential markdown code block wrapping
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = json.loads(text)

    hook = data["hook"]
    body = data["body"]
    cta = data["cta"]
    full_text = f"{hook} {body} {cta}"
    word_count = len(full_text.split())

    return GeneratedScript(
        script_id=uuid4(),
        hook_text=hook,
        body_text=body,
        cta_text=cta,
        full_text=full_text,
        word_count=word_count,
        estimated_duration_s=word_count / 2.5,
        hook_category=HookCategory(data.get("hook_category", "curiosity_gap")),
        hook_template_id=request.hook_template_id,
    )


async def generate_script_for_topic(
    topic: str,
    niche_slug: str,
    platform: str = "tiktok",
    duration_s: int = 45,
    content_format: str = "faceless",
) -> GeneratedScript:
    """Convenience wrapper — generates a script from a topic string.

    Selects a hook template via Thompson Sampling and generates the script.
    """
    from sovi.hooks.selector import select_hook_template

    # Select best hook template
    hook = await select_hook_template(niche_slug=niche_slug, platform=platform)
    logger.info("Selected hook: %s", hook.get("template_text", "none")[:50] if hook else "none")

    try:
        plat = Platform(platform)
    except ValueError:
        plat = Platform.TIKTOK

    request = ScriptRequest(
        topic=TopicCandidate(
            topic=topic,
            niche_slug=niche_slug,
            platform=plat,
        ),
        content_format=ContentFormat(content_format),
        target_duration_s=duration_s,
        target_platforms=[plat],
        hook_template_id=UUID(str(hook["id"])) if hook else None,
    )

    return await generate_script(request, hook_template=hook)
