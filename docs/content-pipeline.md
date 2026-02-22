# Content Pipeline

## Overview

The content pipeline produces short-form video content through a multi-stage process:

```
Research → Script → Assets → Assembly → Quality → Distribution
```

## Niche Configuration

Each niche is defined in `config/niches/{slug}.yaml`:

```yaml
name: "Personal Finance"
slug: personal_finance
tier: "1"                    # Priority tier
platforms:
  - tiktok
  - instagram

content_pillars:
  - budgeting_basics
  - investing_101
  - passive_income
  - debt_elimination
  - money_mindset

hashtags:
  tiktok:
    - personalfinance
    - moneytips
    - investing101
  instagram:
    - financialfreedom
    - moneymanagement
```

**Active Niches:** personal_finance, ai_storytelling, tech_ai_tools, motivation, true_crime

## Research (`src/sovi/research/`)

### Trend Detection (`trend_detector.py`)

Identifies trending topics across platforms using overperformance ratios.

### Scrapers (`scrapers/`)

- **Reddit** (`reddit.py`): PRAW-based scraper for trending posts in niche subreddits
- **TikTok** (`tiktok.py`): TikTok trending content scraper

### Run Scan (`run_scan.py`)

CLI entry point: `sovi research [--reddit-only] [--tiktok-only] [--stories]`

## Production (`src/sovi/production/`)

### Scriptwriter (`scriptwriter.py`)

Uses Claude (Anthropic API) to generate video scripts from trending topics. Produces:
- Hook text (opening line)
- Body text (main content)
- CTA text (call to action)
- Estimated duration

### Asset Generation (`assets/`)

| Module | Provider | Purpose |
|--------|----------|---------|
| `voice_gen.py` | OpenAI TTS / ElevenLabs | Voiceover generation |
| `image_gen.py` | fal.ai (FLUX) | Background images |
| `video_gen.py` | fal.ai (Kling/Hailuo) | Video clips |
| `music.py` | Background music | Ambient audio |
| `transcription.py` | Deepgram Nova-3 | Word-level timestamps for captions |

### Video Formats (`formats/`)

| Format | Description |
|--------|-------------|
| `faceless.py` | AI voiceover + stock/generated visuals |
| `reddit_story.py` | Reddit screenshot narration format |
| `carousel.py` | Multi-slide carousel for Instagram |

### Assembly (`assembly.py`)

FFmpeg-based video assembly:
- Combines voiceover, visuals, music
- Adds auto-generated captions (word-level sync)
- Exports to 1080x1920 vertical format
- H.264 video + AAC audio

### Quality Check (`quality.py`)

Automated QC producing a `QualityReport`:
- Resolution verification
- Bitrate check
- Audio presence
- Caption accuracy
- Safe zone compliance
- Content policy check

### Dry Run (`dry_run.py`)

Pipeline validation without producing assets: `sovi dry-run --topic "..." --niche personal_finance`

## Distribution (`src/sovi/distribution/`)

### Account Selection (`accounts.py`)

- `get_account_for_posting(platform, niche)` — Selects best account (highest followers, respecting 12h cooldown)
- `record_post(account_id, platform)` — Updates `last_post_at`
- `set_account_state(account_id, state)` — State transitions

### Orchestrator (`orchestrator.py`)

Coordinates the full distribution flow:
1. Select trending topic
2. Generate script
3. Produce assets
4. Assemble video
5. Quality check
6. Select account
7. Post to platform

### Poster (`poster.py`)

Platform-specific posting via Late.dev API or direct device automation.

### Scheduler (`scheduler.py`)

Distribution scheduling (distinct from the warming scheduler).

## Pipeline Data Models (`src/sovi/models.py`)

```
TopicCandidate → ScriptRequest → GeneratedScript
                                      ↓
                               AssetSpec → GeneratedAsset
                                      ↓
                              PlatformExport → QualityReport
                                      ↓
                            DistributionRequest → EngagementSnapshot
```

### Key Enums

| Enum | Values |
|------|--------|
| Platform | tiktok, instagram, youtube_shorts, reddit, x_twitter |
| ContentFormat | faceless, reddit_story, ai_avatar, carousel, meme, listicle |
| VideoTier | free, budget, low_mid, mid, premium, cinematic |
| AccountState | created, warming_p1/p2/p3, active, resting, cooldown, flagged, restricted, shadowbanned, suspended, banned |
| ProductionStatus | scripting, generating, assembling, distributing, complete, failed |
| HookCategory | curiosity_gap, bold_claim, problem_pain, proof_results, numbers_data, urgency_fomo, list_structure, personal_story, shock_tension, direct_callout |

## Hooks (`src/sovi/hooks/`)

Video hook (opening line) management:

- `seed_hooks.py` — Seed hook templates into DB
- `selector.py` — Select best hook for a topic/niche combo
- `extractor.py` — Extract hooks from viral content

## Analytics (`src/sovi/analytics/`)

Post-distribution performance tracking:

- `collector.py` — Gather engagement metrics from platforms
- `scorer.py` — Score content performance
- `feedback.py` — Feed performance data back into content selection

## Workflows (`src/sovi/workflows/`)

Temporal workflow definitions (aspirational — requires Temporal server):

- `video_production.py` — Full production workflow
- `activities.py` — Individual workflow activities
- `worker.py` — Temporal worker process
