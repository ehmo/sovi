"""Seed the hooks table with initial hook templates across all 10 categories.

Usage:
    python -m sovi.hooks.seed_hooks
"""

from __future__ import annotations

import logging

import psycopg

from sovi.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# 10 categories × 10+ templates each = 100+ starter hooks
# [TOPIC], [AUDIENCE], [NUMBER], [RESULT], [TIME], [ACTION] are variables
HOOK_TEMPLATES: dict[str, list[str]] = {
    "curiosity_gap": [
        "Nobody talks about this, but [TOPIC] is changing everything",
        "I probably shouldn't share this [TOPIC] secret",
        "The [TOPIC] trick they don't want you to know",
        "Here's what they don't tell you about [TOPIC]",
        "I've been hiding this [TOPIC] hack for months",
        "The real reason [TOPIC] works is not what you think",
        "Most people have no idea that [TOPIC]",
        "This is the [TOPIC] secret that changed my perspective",
        "You've been doing [TOPIC] wrong your entire life",
        "I discovered something about [TOPIC] that blew my mind",
    ],
    "bold_claim": [
        "Everything you knew about [TOPIC] is wrong",
        "[TOPIC] is dead. Here's what's replacing it",
        "[TOPIC] is the biggest scam of [TIME]",
        "I'm going to prove that [TOPIC] doesn't work",
        "[TOPIC] will be completely different in [TIME]",
        "Forget [TOPIC]. This is what actually works",
        "The [TOPIC] industry doesn't want you to see this",
        "[TOPIC] is a lie and here's the proof",
        "After [TIME] of [TOPIC], I finally realized the truth",
        "Hot take: [TOPIC] is overrated and here's why",
    ],
    "problem_pain": [
        "This is why your [TOPIC] isn't working",
        "The #1 mistake [AUDIENCE] make with [TOPIC]",
        "Stop doing this with [TOPIC] immediately",
        "If your [TOPIC] looks like this, we need to talk",
        "[AUDIENCE], you're losing money because of this [TOPIC] mistake",
        "Why [AUDIENCE] keep failing at [TOPIC]",
        "This [TOPIC] mistake is costing you [RESULT]",
        "If you're struggling with [TOPIC], watch this",
        "The reason [TOPIC] feels so hard is because of this",
        "[NUMBER]% of [AUDIENCE] get [TOPIC] completely wrong",
    ],
    "proof_results": [
        "I went from [RESULT] to [RESULT] in just [TIME]",
        "Watch what happens when I try [TOPIC]",
        "I tested [TOPIC] for [TIME] and here are the results",
        "Day [NUMBER] of [TOPIC] — the results are insane",
        "How I [RESULT] using only [TOPIC]",
        "Before and after [TIME] of [TOPIC]",
        "I tried [TOPIC] so you don't have to",
        "Here's what happened after [TIME] of [TOPIC]",
        "Real results: [TOPIC] actually works",
        "[TOPIC] transformation in just [TIME]",
    ],
    "numbers_data": [
        "Why [NUMBER]% of [AUDIENCE] don't [RESULT]",
        "This mistake costs [AUDIENCE] $[NUMBER] every [TIME]",
        "[NUMBER] [TOPIC] facts that will blow your mind",
        "Only [NUMBER]% of people know this about [TOPIC]",
        "I analyzed [NUMBER] [TOPIC] and found this pattern",
        "The [NUMBER]-second rule that changes everything about [TOPIC]",
        "[TOPIC] by the numbers: what the data actually shows",
        "[NUMBER] reasons why [TOPIC] is your biggest opportunity",
        "According to data, [NUMBER]% of [AUDIENCE] should be doing [TOPIC]",
        "The [NUMBER] [TOPIC] stats that shocked me",
    ],
    "urgency_fomo": [
        "Before you [ACTION], you need to know this about [TOPIC]",
        "Only [NUMBER]% of people know about this [TOPIC] hack",
        "This [TOPIC] opportunity won't last much longer",
        "You're running out of time to start [TOPIC]",
        "If you haven't started [TOPIC] yet, here's your sign",
        "The [TOPIC] window is closing — here's why",
        "Stop scrolling if you're serious about [TOPIC]",
        "Last chance to get ahead with [TOPIC]",
        "In [TIME], [TOPIC] will be completely different",
        "Everyone will be talking about [TOPIC] in [TIME]",
    ],
    "list_structure": [
        "[NUMBER] [TOPIC] tips that actually work",
        "[NUMBER] underestimated [TOPIC] strategies for [AUDIENCE]",
        "[NUMBER] things I wish I knew about [TOPIC] sooner",
        "The [NUMBER] best [TOPIC] for [AUDIENCE] in [TIME]",
        "[NUMBER] [TOPIC] mistakes to avoid right now",
        "Top [NUMBER] [TOPIC] that changed my life",
        "[NUMBER] [TOPIC] hacks you'll wish you knew sooner",
        "[NUMBER] signs you need to fix your [TOPIC]",
        "My [NUMBER] favorite [TOPIC] tools",
        "[NUMBER] [TOPIC] rules I live by",
    ],
    "personal_story": [
        "If I had to start [TOPIC] over from scratch",
        "POV: You finally figured out [TOPIC]",
        "How [TOPIC] completely changed my life",
        "I failed at [TOPIC] for [TIME] before discovering this",
        "My honest experience with [TOPIC] after [TIME]",
        "What [TIME] of [TOPIC] taught me",
        "The moment I realized [TOPIC] was the answer",
        "A year ago I started [TOPIC]. Here's what happened",
        "I quit [TOPIC] and here's why",
        "Story time: how [TOPIC] changed everything for me",
    ],
    "shock_tension": [
        "These [NUMBER] [TOPIC] tips feel illegal to know",
        "Don't hate me for sharing this [TOPIC] secret",
        "I can't believe [TOPIC] actually works like this",
        "This [TOPIC] hack should be banned",
        "What I'm about to show you about [TOPIC] is controversial",
        "I got in trouble for sharing this [TOPIC] technique",
        "The [TOPIC] method they tried to keep hidden",
        "Warning: this [TOPIC] content may change your perspective",
        "I wasn't supposed to share this about [TOPIC]",
        "The dark side of [TOPIC] nobody talks about",
    ],
    "direct_callout": [
        "Stop scrolling [AUDIENCE] — you need to hear this about [TOPIC]",
        "[AUDIENCE], this is for you",
        "If you're [AUDIENCE] trying to [RESULT], watch this",
        "Attention [AUDIENCE]: [TOPIC] is about to change",
        "Hey [AUDIENCE], here's the [TOPIC] advice no one gives you",
        "[AUDIENCE] who want [RESULT] — bookmark this",
        "This is the [TOPIC] video [AUDIENCE] has been waiting for",
        "Every [AUDIENCE] needs to know about [TOPIC]",
        "[AUDIENCE] struggling with [TOPIC] — I see you",
        "Calling all [AUDIENCE]: [TOPIC] doesn't have to be hard",
    ],
}

# Emotional tones per category
CATEGORY_TONES = {
    "curiosity_gap": "curiosity",
    "bold_claim": "contrarian",
    "problem_pain": "empathy",
    "proof_results": "inspiration",
    "numbers_data": "authority",
    "urgency_fomo": "urgency",
    "list_structure": "practical",
    "personal_story": "vulnerability",
    "shock_tension": "excitement",
    "direct_callout": "connection",
}


def seed_hooks() -> int:
    """Insert all hook templates into the database."""
    count = 0
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            for category, templates in HOOK_TEMPLATES.items():
                tone = CATEGORY_TONES.get(category, "neutral")
                for template in templates:
                    cur.execute(
                        """INSERT INTO hooks
                           (hook_text, template_text, hook_category, emotional_tone,
                            thompson_alpha, thompson_beta)
                           VALUES (%s, %s, %s, %s, 1.0, 1.0)
                           ON CONFLICT DO NOTHING""",
                        (template, template, category, tone),
                    )
                    count += 1
            conn.commit()
    return count


if __name__ == "__main__":
    inserted = seed_hooks()
    logger.info("Seeded %d hook templates across %d categories", inserted, len(HOOK_TEMPLATES))
