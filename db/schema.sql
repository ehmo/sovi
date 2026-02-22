-- =============================================================================
-- SOVI: Social Video Intelligence — PostgreSQL + TimescaleDB Schema
-- =============================================================================
--
-- System overview:
--   50+ accounts across TikTok, Instagram, YouTube Shorts, Reddit, X/Twitter
--   50+ videos/day production pipeline
--   Time-series engagement tracking at multiple checkpoints
--   Device farm management with USB/Appium orchestration
--   Thompson Sampling for hook template optimization
--
-- Design principles:
--   1. ENUM types for closed sets that change rarely (platforms, statuses)
--   2. JSONB for semi-structured data that varies per entity (voice profiles,
--      platform-specific settings, API responses)
--   3. TimescaleDB hypertables for time-series data (metrics, activity logs)
--   4. Partial indexes for the most common filtered queries
--   5. Encrypted columns noted with comments; actual encryption handled at
--      the application layer (pgcrypto or app-level AES-256-GCM)
--   6. All timestamps are TIMESTAMPTZ (timezone-aware)
--   7. Soft deletes via deleted_at where appropriate
--
-- Naming conventions:
--   - Tables: snake_case, plural nouns
--   - Columns: snake_case
--   - Indexes: idx_{table}_{column(s)}
--   - Foreign keys: fk_{table}_{referenced_table}
--   - Constraints: chk_{table}_{description}, uq_{table}_{column(s)}
--
-- =============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";      -- UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";        -- Encryption helpers
CREATE EXTENSION IF NOT EXISTS timescaledb;       -- Time-series engine

-- =============================================================================
-- ENUM TYPES
-- =============================================================================

CREATE TYPE platform_type AS ENUM (
    'tiktok',
    'instagram',
    'youtube_shorts',
    'reddit',
    'x_twitter'
);

CREATE TYPE account_status AS ENUM (
    'created',          -- Account created, not yet warmed
    'warming',          -- In warm-up phase (days 1-14)
    'active',           -- Fully operational, posting content
    'cooling',          -- Temporarily paused to reduce risk
    'shadowbanned',     -- Detected shadowban, in recovery
    'restricted',       -- Platform-imposed action block
    'suspended',        -- Full suspension, may be recoverable
    'banned',           -- Permanent ban, unrecoverable
    'retired',          -- Voluntarily retired
    'bench'             -- Warmed and ready, held in reserve
);

CREATE TYPE warming_phase AS ENUM (
    'browsing',         -- Days 1-2: passive consumption
    'engaging',         -- Days 3-4: likes, follows, comments
    'deepening',        -- Days 5-7: saves, shares, more engagement
    'first_post',       -- Days 8-10: first content posted
    'ramping',          -- Days 11-14: increasing post frequency
    'complete'          -- Warm-up finished
);

CREATE TYPE content_status AS ENUM (
    'draft',            -- Script written, not yet produced
    'scripted',         -- Script approved and finalized
    'producing',        -- Assets being assembled
    'rendered',         -- Video rendered, awaiting QA
    'qa_passed',        -- Quality gate passed
    'qa_failed',        -- Quality gate failed, needs rework
    'queued',           -- In publishing queue
    'published',        -- Posted to at least one platform
    'recycling',        -- Being remixed for repost
    'retired'           -- No longer in rotation
);

CREATE TYPE distribution_status AS ENUM (
    'pending',          -- Awaiting scheduled time
    'uploading',        -- Upload in progress
    'published',        -- Successfully posted
    'failed',           -- Upload or posting failed
    'removed',          -- Removed by us
    'taken_down',       -- Removed by platform (strike/violation)
    'scheduled'         -- Scheduled on platform's native scheduler
);

CREATE TYPE asset_type AS ENUM (
    'stock_footage',
    'ai_generated_image',
    'ai_generated_video',
    'screen_recording',
    'voiceover',
    'music_track',
    'sound_effect',
    'subtitle_file',
    'thumbnail',
    'font',
    'template_graphic'
);

CREATE TYPE content_pillar_type AS ENUM (
    'educational',
    'entertainment',
    'inspirational',
    'promotional'
);

CREATE TYPE hook_category AS ENUM (
    'curiosity_gap',
    'direct_callout',
    'controversy',
    'statistical_shock',
    'story_tease',
    'challenge',
    'value_promise',
    'trending_adaptation'
);

CREATE TYPE activity_action AS ENUM (
    'watch',
    'like',
    'unlike',
    'comment',
    'reply_comment',
    'follow',
    'unfollow',
    'share',
    'save',
    'post',
    'repost',
    'delete_post',
    'browse_fyp',
    'search',
    'view_profile',
    'dm_send',
    'stitch',
    'duet',
    'pin_comment',
    'edit_profile'
);

CREATE TYPE device_status AS ENUM (
    'available',        -- Ready for use
    'in_use',           -- Currently running an automation session
    'disconnected',     -- USB disconnected or unreachable
    'error',            -- Error state, needs attention
    'maintenance',      -- Undergoing maintenance or factory reset
    'retired'           -- No longer in service
);

CREATE TYPE content_lifecycle AS ENUM (
    'flash',            -- High initial views, rapid decay
    'slow_burn',        -- Gradual growth over days/weeks
    'evergreen',        -- Sustained views over months
    'resurging'         -- Spiked again after initial decay
);

CREATE TYPE metric_checkpoint AS ENUM (
    't_1h',
    't_6h',
    't_24h',
    't_48h',
    't_7d',
    't_30d'
);

-- =============================================================================
-- TABLE: niches
-- =============================================================================
-- Central niche configuration. Each niche defines a content vertical with its
-- own voice, topics, hashtags, and subreddit targets. One niche maps to many
-- accounts across platforms.

CREATE TABLE niches (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL UNIQUE,            -- e.g. 'personal_finance'
    display_name    TEXT NOT NULL,                   -- e.g. 'Personal Finance'
    tier            SMALLINT NOT NULL DEFAULT 2,     -- 1=highest potential, 2=strong, 3=experimental
    is_active       BOOLEAN NOT NULL DEFAULT true,

    -- Voice profile: fed into LLM system prompt for script generation
    voice_profile   JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Schema: { tone, formality (0-100), humor_level (0-100), energy,
    --           perspective, banned_phrases[], signature_phrases[], example }

    -- Content pillars for this niche (3-5 recommended)
    content_pillars JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Schema: [{ name, type (content_pillar_type), description, weight }]

    -- Hashtag strategy
    hashtag_sets    JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Schema: [{ name, tags[], tier (popular|niche|micro), last_rotated_at }]

    -- Subreddit targets
    subreddits      JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Schema: [{ name, subscriber_count, is_primary, rules_notes, min_karma,
    --            min_account_age_days }]

    -- Niche-specific content settings
    best_format         TEXT,               -- e.g. 'text_overlay_charts_voiceover'
    best_length_seconds INT4RANGE,          -- e.g. [30,60]
    content_ratio       JSONB NOT NULL DEFAULT '{"educational":35,"entertainment":35,"inspirational":20,"promotional":10}'::jsonb,

    -- Competitor intelligence
    competitor_data JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Schema: [{ handle, platform, followers, avg_views, notes }]

    -- Estimated CPM range for this niche
    cpm_low         NUMERIC(8,2),
    cpm_high        NUMERIC(8,2),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_niches_active ON niches (is_active) WHERE is_active = true;
CREATE INDEX idx_niches_tier ON niches (tier);

-- =============================================================================
-- TABLE: proxies
-- =============================================================================
-- Residential/mobile proxies. Each proxy is assigned to 1-2 accounts max.
-- Never share IPs between accounts to avoid cross-contamination.

CREATE TABLE proxies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    label           TEXT NOT NULL,
    provider        TEXT NOT NULL,                   -- e.g. 'brightdata', 'oxylabs'
    proxy_type      TEXT NOT NULL DEFAULT 'residential',  -- residential, mobile, datacenter

    -- Connection details — ENCRYPTED at application layer
    host            TEXT NOT NULL,                   -- ENCRYPTED
    port            INT NOT NULL,
    username        TEXT,                            -- ENCRYPTED
    password        TEXT,                            -- ENCRYPTED
    protocol        TEXT NOT NULL DEFAULT 'socks5',  -- socks5, http, https

    -- Geographic targeting
    country_code    CHAR(2),                         -- ISO 3166-1 alpha-2
    city            TEXT,
    asn             TEXT,

    -- Health
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_checked_at TIMESTAMPTZ,
    last_latency_ms INT,
    failure_count   INT NOT NULL DEFAULT 0,

    -- Rotation
    rotate_interval_hours INT,                       -- NULL = sticky session

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_proxies_active ON proxies (is_active) WHERE is_active = true;
CREATE INDEX idx_proxies_country ON proxies (country_code);

-- =============================================================================
-- TABLE: devices
-- =============================================================================
-- Physical iPhone devices connected via USB for Appium-based automation.
-- Each device has its own fingerprint and proxy assignment.

CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    label           TEXT NOT NULL UNIQUE,             -- e.g. 'iphone-01'
    model           TEXT NOT NULL,                    -- e.g. 'iPhone 14 Pro'
    ios_version     TEXT,
    udid            TEXT NOT NULL UNIQUE,             -- Unique Device Identifier

    -- Connection
    usb_port        TEXT,                             -- Physical USB port identifier
    status          device_status NOT NULL DEFAULT 'available',
    last_heartbeat  TIMESTAMPTZ,                     -- Last successful health check

    -- Appium session management
    appium_port     INT,                             -- Port for this device's Appium server
    appium_session_id TEXT,                           -- Current WDA session ID
    wda_bundle_id   TEXT DEFAULT 'com.facebook.WebDriverAgentRunner.xctrunner',

    -- Proxy assignment (1 proxy per device)
    proxy_id        UUID REFERENCES proxies(id) ON DELETE SET NULL,

    -- Device fingerprint data
    fingerprint     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Schema: { screen_resolution, timezone, language, carrier, wifi_mac }

    -- Capacity tracking
    max_accounts    SMALLINT NOT NULL DEFAULT 2,     -- Max accounts on this device
    current_accounts SMALLINT NOT NULL DEFAULT 0,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_devices_status ON devices (status);
CREATE INDEX idx_devices_available ON devices (status) WHERE status = 'available';
CREATE INDEX idx_devices_proxy ON devices (proxy_id);

-- =============================================================================
-- TABLE: accounts
-- =============================================================================
-- Social media accounts. Each account belongs to one platform, one niche,
-- and is assigned a device and proxy for isolation.

CREATE TABLE accounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    platform        platform_type NOT NULL,
    niche_id        UUID NOT NULL REFERENCES niches(id) ON DELETE RESTRICT,

    -- Identity
    username        TEXT NOT NULL,
    display_name    TEXT,
    email           TEXT,                            -- ENCRYPTED
    phone           TEXT,                            -- ENCRYPTED
    bio             TEXT,
    profile_photo_url TEXT,

    -- Credentials — ALL ENCRYPTED at application layer
    password        TEXT NOT NULL,                   -- ENCRYPTED (AES-256-GCM)
    totp_secret     TEXT,                            -- ENCRYPTED (for 2FA)
    session_tokens  JSONB DEFAULT '{}'::jsonb,       -- ENCRYPTED (platform cookies/tokens)
    api_keys        JSONB DEFAULT '{}'::jsonb,       -- ENCRYPTED (platform API keys if any)

    -- Infrastructure
    device_id       UUID REFERENCES devices(id) ON DELETE SET NULL,
    proxy_id        UUID REFERENCES proxies(id) ON DELETE SET NULL,
    browser_profile_id TEXT,                         -- GoLogin/Multilogin profile ID

    -- Status and warming
    status          account_status NOT NULL DEFAULT 'created',
    warming_phase   warming_phase,
    warming_started_at TIMESTAMPTZ,
    warming_completed_at TIMESTAMPTZ,

    -- Health scoring (0-100, computed from recent metrics)
    health_score    SMALLINT DEFAULT 50,
    last_health_check TIMESTAMPTZ,
    -- Factors: engagement rate vs avg, FYP traffic %, growth rate, shadowban signals
    health_factors  JSONB DEFAULT '{}'::jsonb,
    -- Schema: { engagement_rate, fyp_pct, growth_rate, shadowban_risk, last_strike }

    -- Platform metrics (latest snapshot)
    followers       INT NOT NULL DEFAULT 0,
    following       INT NOT NULL DEFAULT 0,
    total_posts     INT NOT NULL DEFAULT 0,
    total_likes     BIGINT NOT NULL DEFAULT 0,

    -- Cadence and scheduling
    posts_today     SMALLINT NOT NULL DEFAULT 0,
    max_posts_per_day SMALLINT NOT NULL DEFAULT 2,
    min_post_gap_minutes INT NOT NULL DEFAULT 240,   -- 4 hours between posts
    last_posted_at  TIMESTAMPTZ,

    -- Daily activity budget (for warming/engagement automation)
    daily_likes_budget   SMALLINT NOT NULL DEFAULT 10,
    daily_comments_budget SMALLINT NOT NULL DEFAULT 5,
    daily_follows_budget  SMALLINT NOT NULL DEFAULT 5,

    -- Cost tracking
    monthly_proxy_cost   NUMERIC(8,2) DEFAULT 0,
    monthly_tool_cost    NUMERIC(8,2) DEFAULT 0,     -- antidetect browser, etc.

    -- Lifecycle
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ,                     -- Soft delete

    -- Constraints
    CONSTRAINT uq_accounts_platform_username UNIQUE (platform, username),
    CONSTRAINT chk_accounts_health_score CHECK (health_score BETWEEN 0 AND 100)
);

-- "Get all active accounts for platform X"
CREATE INDEX idx_accounts_platform_status ON accounts (platform, status);
CREATE INDEX idx_accounts_active_platform ON accounts (platform)
    WHERE status = 'active' AND deleted_at IS NULL;

-- "Get all accounts in warming phase that need attention"
CREATE INDEX idx_accounts_warming ON accounts (warming_phase, warming_started_at)
    WHERE status = 'warming';

CREATE INDEX idx_accounts_niche ON accounts (niche_id);
CREATE INDEX idx_accounts_device ON accounts (device_id);
CREATE INDEX idx_accounts_proxy ON accounts (proxy_id);
CREATE INDEX idx_accounts_health ON accounts (health_score) WHERE deleted_at IS NULL;
CREATE INDEX idx_accounts_last_posted ON accounts (last_posted_at);

-- =============================================================================
-- TABLE: hook_templates
-- =============================================================================
-- Hook templates with Thompson Sampling (Beta distribution) for multi-armed
-- bandit optimization. Each template has alpha/beta params that update as
-- content using that hook succeeds or fails.

CREATE TABLE hook_templates (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    niche_id        UUID REFERENCES niches(id) ON DELETE SET NULL,
    -- NULL niche_id = universal template usable across niches

    category        hook_category NOT NULL,
    name            TEXT NOT NULL,                   -- Short label, e.g. 'curiosity_90pct'
    template_text   TEXT NOT NULL,                   -- The hook template with {placeholders}
    -- Example: "Only {percentage}% of people know this about {topic}..."

    -- Thompson Sampling parameters (Beta distribution)
    -- alpha = successes + 1 (prior), beta = failures + 1 (prior)
    -- To sample: draw from Beta(alpha, beta), pick the template with highest draw
    ts_alpha        NUMERIC(12,4) NOT NULL DEFAULT 1.0,
    ts_beta         NUMERIC(12,4) NOT NULL DEFAULT 1.0,

    -- Derived scoring (updated periodically from ts_alpha/ts_beta)
    win_rate        NUMERIC(8,6) DEFAULT 0.5,        -- alpha / (alpha + beta)
    total_trials    INT NOT NULL DEFAULT 0,
    total_successes INT NOT NULL DEFAULT 0,

    -- A/B testing
    variant_group   TEXT,                            -- Groups variants for A/B testing
    -- e.g. 'curiosity_v1' / 'curiosity_v2' share the same variant_group

    -- Performance stats (aggregated)
    avg_completion_rate NUMERIC(6,4),                -- 0.0 - 1.0
    avg_engagement_rate NUMERIC(6,4),
    sample_size     INT NOT NULL DEFAULT 0,

    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Find top-performing hook templates in niche Z"
CREATE INDEX idx_hooks_niche_performance ON hook_templates (niche_id, win_rate DESC)
    WHERE is_active = true;
CREATE INDEX idx_hooks_category ON hook_templates (category);
CREATE INDEX idx_hooks_variant ON hook_templates (variant_group)
    WHERE variant_group IS NOT NULL;
CREATE INDEX idx_hooks_active ON hook_templates (is_active, niche_id)
    WHERE is_active = true;

-- =============================================================================
-- TABLE: scripts
-- =============================================================================
-- Generated scripts for content. Each script belongs to a niche and uses a
-- hook template. Scripts go through the content pipeline to become videos.

CREATE TABLE scripts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    niche_id        UUID NOT NULL REFERENCES niches(id) ON DELETE RESTRICT,
    hook_template_id UUID REFERENCES hook_templates(id) ON DELETE SET NULL,

    -- Content classification
    content_pillar  content_pillar_type,
    topic           TEXT NOT NULL,
    title           TEXT NOT NULL,                   -- Internal title for tracking

    -- Script content
    hook_text       TEXT NOT NULL,                   -- The actual hook (first 3 seconds)
    body_text       TEXT NOT NULL,                   -- Main content
    cta_text        TEXT,                            -- Call to action
    caption         TEXT,                            -- Post caption
    hashtags        TEXT[],                          -- Array of hashtags

    -- Generation metadata
    llm_model       TEXT,                            -- e.g. 'gpt-4o', 'claude-opus-4-5'
    llm_prompt_hash TEXT,                            -- Hash of system+user prompt for reproducibility
    generation_params JSONB DEFAULT '{}'::jsonb,
    -- Schema: { temperature, max_tokens, voice_profile_snapshot }

    -- Quality gate
    quality_score   NUMERIC(5,2),                   -- Automated quality score 0-100
    quality_flags   TEXT[],                          -- e.g. ['hook_too_long', 'banned_phrase_detected']
    approved        BOOLEAN NOT NULL DEFAULT false,

    -- Estimated duration in seconds
    estimated_duration_secs INT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_scripts_niche ON scripts (niche_id);
CREATE INDEX idx_scripts_hook ON scripts (hook_template_id);
CREATE INDEX idx_scripts_pillar ON scripts (content_pillar);
CREATE INDEX idx_scripts_approved ON scripts (approved) WHERE approved = true;

-- =============================================================================
-- TABLE: assets
-- =============================================================================
-- Media assets: stock footage, AI-generated images/video, voiceovers, music,
-- thumbnails, etc. Assets are reusable across multiple content pieces.

CREATE TABLE assets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    asset_type      asset_type NOT NULL,
    niche_id        UUID REFERENCES niches(id) ON DELETE SET NULL,

    -- File storage
    file_path       TEXT NOT NULL,                   -- Path in object storage (S3/GCS)
    file_size_bytes BIGINT,
    mime_type       TEXT,
    duration_secs   NUMERIC(8,2),                   -- For audio/video assets
    resolution      TEXT,                            -- e.g. '1080x1920'

    -- Metadata
    title           TEXT,
    description     TEXT,
    tags            TEXT[],
    source          TEXT,                            -- e.g. 'pexels', 'elevenlabs', 'kling'
    source_url      TEXT,
    license         TEXT,                            -- e.g. 'pexels_free', 'custom'

    -- Generation metadata (for AI-generated assets)
    generation_params JSONB DEFAULT '{}'::jsonb,
    -- Schema: { model, prompt, seed, style, voice_id }

    -- Usage tracking
    times_used      INT NOT NULL DEFAULT 0,
    last_used_at    TIMESTAMPTZ,

    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_assets_type ON assets (asset_type);
CREATE INDEX idx_assets_niche ON assets (niche_id);
CREATE INDEX idx_assets_tags ON assets USING GIN (tags);

-- =============================================================================
-- TABLE: contents
-- =============================================================================
-- The assembled video content piece. This is the core entity that flows through
-- the pipeline: script -> asset assembly -> rendering -> QA -> publishing.
-- One content piece can be distributed to multiple platforms.

CREATE TABLE contents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    niche_id        UUID NOT NULL REFERENCES niches(id) ON DELETE RESTRICT,
    script_id       UUID REFERENCES scripts(id) ON DELETE SET NULL,

    -- Content identity
    title           TEXT NOT NULL,
    status          content_status NOT NULL DEFAULT 'draft',

    -- Assembled video
    video_path      TEXT,                            -- Final rendered video in object storage
    video_size_bytes BIGINT,
    duration_secs   NUMERIC(8,2),
    resolution      TEXT DEFAULT '1080x1920',        -- 9:16 portrait
    fps             SMALLINT DEFAULT 30,

    -- Content metadata
    content_pillar  content_pillar_type,
    hook_template_id UUID REFERENCES hook_templates(id) ON DELETE SET NULL,
    topic           TEXT,
    hashtags        TEXT[],

    -- Classification (set after performance data is available)
    lifecycle_class content_lifecycle,
    -- flash: >80% of views in first 24h
    -- slow_burn: views still growing after 48h
    -- evergreen: >20% of views come after 7d
    -- resurging: view velocity increased after initial decay

    -- Overperformance ratio: actual_views / expected_views_for_account
    overperformance_ratio NUMERIC(8,4),

    -- Production cost tracking
    production_cost_usd NUMERIC(8,2) DEFAULT 0,     -- LLM + TTS + rendering costs
    -- Breakdown stored in JSONB for flexibility
    cost_breakdown  JSONB DEFAULT '{}'::jsonb,
    -- Schema: { llm: 0.03, tts: 0.05, stock_footage: 0, rendering: 0.01 }

    -- Recycling / remix tracking
    original_content_id UUID REFERENCES contents(id) ON DELETE SET NULL,
    remix_count     SMALLINT NOT NULL DEFAULT 0,
    last_recycled_at TIMESTAMPTZ,

    -- Quality gate
    qa_passed       BOOLEAN,
    qa_notes        TEXT,
    qa_checked_at   TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_contents_niche ON contents (niche_id);
CREATE INDEX idx_contents_status ON contents (status);
CREATE INDEX idx_contents_lifecycle ON contents (lifecycle_class) WHERE lifecycle_class IS NOT NULL;
CREATE INDEX idx_contents_overperf ON contents (overperformance_ratio DESC NULLS LAST)
    WHERE overperformance_ratio IS NOT NULL;
CREATE INDEX idx_contents_hook ON contents (hook_template_id);
CREATE INDEX idx_contents_created ON contents (created_at DESC);
CREATE INDEX idx_contents_original ON contents (original_content_id)
    WHERE original_content_id IS NOT NULL;

-- =============================================================================
-- TABLE: content_assets
-- =============================================================================
-- Join table: which assets were used in which content piece.

CREATE TABLE content_assets (
    content_id      UUID NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    asset_id        UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,

    -- Usage context within the content
    role            TEXT NOT NULL DEFAULT 'footage',  -- footage, voiceover, music, subtitle, thumbnail
    start_time_secs NUMERIC(8,2),                    -- Where in the video this asset appears
    end_time_secs   NUMERIC(8,2),

    PRIMARY KEY (content_id, asset_id, role)
);

CREATE INDEX idx_content_assets_asset ON content_assets (asset_id);

-- =============================================================================
-- TABLE: platform_exports
-- =============================================================================
-- Platform-specific versions of a content piece. The same content may need
-- different formatting, captions, hashtags, and metadata per platform.

CREATE TABLE platform_exports (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_id      UUID NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    platform        platform_type NOT NULL,

    -- Platform-specific file (may differ from base content)
    video_path      TEXT,                            -- Platform-formatted version
    thumbnail_path  TEXT,

    -- Platform-specific metadata
    title           TEXT,                            -- YouTube Shorts title
    caption         TEXT,
    hashtags        TEXT[],
    description     TEXT,                            -- YouTube description
    tags            TEXT[],                          -- YouTube tags

    -- Platform-specific settings
    scheduled_sound TEXT,                            -- TikTok trending sound
    cover_timestamp NUMERIC(8,2),                   -- Timestamp for cover frame

    -- Format compliance
    watermark_removed BOOLEAN NOT NULL DEFAULT true,
    format_validated  BOOLEAN NOT NULL DEFAULT false,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_platform_exports UNIQUE (content_id, platform)
);

CREATE INDEX idx_platform_exports_platform ON platform_exports (platform);
CREATE INDEX idx_platform_exports_content ON platform_exports (content_id);

-- =============================================================================
-- TABLE: distributions
-- =============================================================================
-- Tracks the actual posting of content to platforms via specific accounts.
-- This is the scheduling and distribution layer.

CREATE TABLE distributions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_id      UUID NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    platform_export_id UUID REFERENCES platform_exports(id) ON DELETE SET NULL,
    account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform        platform_type NOT NULL,

    -- Scheduling
    scheduled_at    TIMESTAMPTZ,                     -- When to post
    published_at    TIMESTAMPTZ,                     -- When actually posted

    -- Status tracking
    status          distribution_status NOT NULL DEFAULT 'pending',
    retry_count     SMALLINT NOT NULL DEFAULT 0,
    max_retries     SMALLINT NOT NULL DEFAULT 3,
    last_error      TEXT,
    error_details   JSONB,

    -- Platform response
    platform_post_id TEXT,                           -- Platform's ID for the post
    platform_url    TEXT,                            -- Direct URL to the post

    -- Staggering logic
    -- Distribution engine checks: account cadence limits, min gap between posts,
    -- max posts per day, and cross-platform stagger windows
    stagger_group   UUID,                            -- Groups distributions that should be staggered
    stagger_order   SMALLINT,                        -- Order within the stagger group

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_distributions_account_content UNIQUE (account_id, content_id)
);

CREATE INDEX idx_distributions_scheduled ON distributions (scheduled_at)
    WHERE status = 'pending';
CREATE INDEX idx_distributions_account ON distributions (account_id, status);
CREATE INDEX idx_distributions_content ON distributions (content_id);
CREATE INDEX idx_distributions_platform ON distributions (platform, status);
CREATE INDEX idx_distributions_stagger ON distributions (stagger_group, stagger_order)
    WHERE stagger_group IS NOT NULL;

-- =============================================================================
-- TABLE: engagement_snapshots  (TimescaleDB Hypertable)
-- =============================================================================
-- Time-series engagement data captured at defined checkpoints after posting.
-- Each row = one snapshot for one distribution at one checkpoint.
--
-- Hypertable: Yes — this is the primary time-series table.
-- Chunk interval: 1 day (we expect ~300 snapshots/day at 50 contents * 6 checkpoints)
-- Compression: After 7 days (most analysis is on recent data)
-- Retention: 1 year (drop chunks older than 365 days)

CREATE TABLE engagement_snapshots (
    -- Time column must be first for TimescaleDB
    captured_at     TIMESTAMPTZ NOT NULL,

    distribution_id UUID NOT NULL REFERENCES distributions(id) ON DELETE CASCADE,
    content_id      UUID NOT NULL,                   -- Denormalized for faster queries
    account_id      UUID NOT NULL,                   -- Denormalized
    platform        platform_type NOT NULL,           -- Denormalized
    checkpoint      metric_checkpoint NOT NULL,

    -- Core metrics (platform-specific, not all platforms report all fields)
    views           BIGINT DEFAULT 0,
    likes           BIGINT DEFAULT 0,
    comments        BIGINT DEFAULT 0,
    shares          BIGINT DEFAULT 0,
    saves           BIGINT DEFAULT 0,
    reach           BIGINT,                          -- Unique viewers (IG/TT)
    impressions     BIGINT,                          -- Total impressions

    -- Engagement quality
    completion_rate NUMERIC(6,4),                    -- 0.0 - 1.0, what % watched to end
    avg_watch_secs  NUMERIC(8,2),
    replay_count    BIGINT DEFAULT 0,

    -- Traffic sources (as percentages)
    fyp_pct         NUMERIC(5,2),                    -- For You Page / Explore
    search_pct      NUMERIC(5,2),
    profile_pct     NUMERIC(5,2),
    hashtag_pct     NUMERIC(5,2),
    other_pct       NUMERIC(5,2),

    -- Follower impact
    follows_gained  INT DEFAULT 0,
    profile_visits  INT DEFAULT 0,

    -- Reddit-specific (nullable for other platforms)
    upvotes         INT,
    downvotes       INT,
    upvote_ratio    NUMERIC(5,4),
    reddit_awards   INT,

    -- X/Twitter-specific
    retweets        INT,
    quote_tweets    INT,
    bookmarks       INT,

    -- Derived metrics (computed on insert or by continuous aggregate)
    engagement_rate NUMERIC(8,6),
    -- Formula: (likes + comments + shares + saves) / views

    -- Monotonically increasing ID for deduplication
    snapshot_id     BIGSERIAL
);

-- Convert to hypertable: chunk by 1 day on captured_at
SELECT create_hypertable(
    'engagement_snapshots',
    by_range('captured_at', INTERVAL '1 day')
);

-- Primary lookup: get performance over time for a specific content piece
CREATE INDEX idx_snapshots_content_time ON engagement_snapshots (content_id, captured_at DESC);
CREATE INDEX idx_snapshots_distribution ON engagement_snapshots (distribution_id, checkpoint);
CREATE INDEX idx_snapshots_platform_time ON engagement_snapshots (platform, captured_at DESC);
CREATE INDEX idx_snapshots_account ON engagement_snapshots (account_id, captured_at DESC);

-- =============================================================================
-- TABLE: activity_logs  (TimescaleDB Hypertable)
-- =============================================================================
-- Every automation action: watch, like, comment, follow, post, etc.
-- High-volume: 50+ accounts * 20+ actions/day = 1000+ rows/day.
--
-- Hypertable: Yes — append-only time-series log.
-- Chunk interval: 1 day
-- Compression: After 3 days (mainly used for real-time monitoring)
-- Retention: 90 days (older data summarized in aggregates)

CREATE TABLE activity_logs (
    performed_at    TIMESTAMPTZ NOT NULL,

    account_id      UUID NOT NULL,                   -- Which account performed the action
    device_id       UUID,                            -- Which device was used
    platform        platform_type NOT NULL,

    action          activity_action NOT NULL,
    success         BOOLEAN NOT NULL DEFAULT true,

    -- Context
    target_url      TEXT,                            -- URL of the target (post, profile, etc.)
    target_username TEXT,                            -- Target user (for follow, comment, etc.)
    content_id      UUID,                            -- If action relates to our content
    distribution_id UUID,                            -- If action is posting our content

    -- Action details
    details         JSONB DEFAULT '{}'::jsonb,
    -- Schema varies by action:
    -- comment: { text, parent_comment_id }
    -- watch: { duration_secs, completed }
    -- search: { query }
    -- post: { platform_post_id, caption }

    -- Error tracking
    error_code      TEXT,
    error_message   TEXT,

    -- Duration of the action (for pacing analysis)
    duration_ms     INT,

    -- Session tracking
    session_id      UUID,                            -- Groups actions in one automation session

    -- Monotonically increasing ID
    log_id          BIGSERIAL
);

SELECT create_hypertable(
    'activity_logs',
    by_range('performed_at', INTERVAL '1 day')
);

CREATE INDEX idx_activity_account_time ON activity_logs (account_id, performed_at DESC);
CREATE INDEX idx_activity_action ON activity_logs (action, performed_at DESC);
CREATE INDEX idx_activity_device ON activity_logs (device_id, performed_at DESC)
    WHERE device_id IS NOT NULL;
CREATE INDEX idx_activity_session ON activity_logs (session_id)
    WHERE session_id IS NOT NULL;
CREATE INDEX idx_activity_failures ON activity_logs (account_id, performed_at DESC)
    WHERE success = false;

-- =============================================================================
-- TABLE: account_health_history  (TimescaleDB Hypertable)
-- =============================================================================
-- Periodic health score snapshots for accounts. Enables trend analysis of
-- account health over time (detecting slow decline before it becomes critical).
--
-- Chunk interval: 7 days (one snapshot per account per day = ~50 rows/day)
-- Compression: After 14 days
-- Retention: 180 days

CREATE TABLE account_health_history (
    recorded_at     TIMESTAMPTZ NOT NULL,

    account_id      UUID NOT NULL,
    platform        platform_type NOT NULL,

    health_score    SMALLINT NOT NULL,
    status          account_status NOT NULL,

    -- Breakdown
    followers       INT,
    engagement_rate NUMERIC(8,6),
    fyp_traffic_pct NUMERIC(5,2),
    growth_rate     NUMERIC(8,4),                    -- Followers gained / day
    shadowban_risk  NUMERIC(5,2),                    -- 0-100 risk score
    posts_last_24h  SMALLINT,
    actions_last_24h SMALLINT,
    failures_last_24h SMALLINT,

    -- Rolling averages
    avg_views_7d    NUMERIC(12,2),
    avg_views_30d   NUMERIC(12,2),
    views_trend     NUMERIC(8,4),                    -- 7d avg / 30d avg (>1 = improving)

    health_id       BIGSERIAL
);

SELECT create_hypertable(
    'account_health_history',
    by_range('recorded_at', INTERVAL '7 days')
);

CREATE INDEX idx_health_account_time ON account_health_history (account_id, recorded_at DESC);
CREATE INDEX idx_health_score ON account_health_history (health_score, recorded_at DESC);

-- =============================================================================
-- TABLE: cost_records
-- =============================================================================
-- Track per-content and per-day production costs for financial analysis.

CREATE TABLE cost_records (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    recorded_date   DATE NOT NULL DEFAULT CURRENT_DATE,

    -- What incurred the cost
    content_id      UUID REFERENCES contents(id) ON DELETE SET NULL,
    niche_id        UUID REFERENCES niches(id) ON DELETE SET NULL,

    -- Cost categories
    category        TEXT NOT NULL,                   -- 'llm', 'tts', 'stock', 'rendering', 'proxy', 'tools', 'device'
    amount_usd      NUMERIC(10,4) NOT NULL,
    quantity        NUMERIC(10,2) DEFAULT 1,         -- e.g. tokens, minutes, API calls
    unit            TEXT,                            -- e.g. 'tokens', 'minutes', 'calls'

    -- Provider
    provider        TEXT,                            -- e.g. 'openai', 'elevenlabs', 'brightdata'
    details         JSONB DEFAULT '{}'::jsonb,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Daily content production cost summary"
CREATE INDEX idx_costs_date ON cost_records (recorded_date DESC);
CREATE INDEX idx_costs_category ON cost_records (category, recorded_date DESC);
CREATE INDEX idx_costs_niche ON cost_records (niche_id, recorded_date DESC);
CREATE INDEX idx_costs_content ON cost_records (content_id) WHERE content_id IS NOT NULL;


-- =============================================================================
-- TIMESCALEDB: COMPRESSION POLICIES
-- =============================================================================
-- Compress old chunks to save storage. Compressed data is still queryable
-- but cannot be updated (which is fine for immutable time-series data).

-- Engagement snapshots: compress after 7 days
ALTER TABLE engagement_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'content_id, platform',
    timescaledb.compress_orderby = 'captured_at DESC'
);
SELECT add_compression_policy('engagement_snapshots', INTERVAL '7 days');

-- Activity logs: compress after 3 days
ALTER TABLE activity_logs SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'account_id, platform',
    timescaledb.compress_orderby = 'performed_at DESC'
);
SELECT add_compression_policy('activity_logs', INTERVAL '3 days');

-- Account health history: compress after 14 days
ALTER TABLE account_health_history SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'account_id',
    timescaledb.compress_orderby = 'recorded_at DESC'
);
SELECT add_compression_policy('account_health_history', INTERVAL '14 days');


-- =============================================================================
-- TIMESCALEDB: RETENTION POLICIES
-- =============================================================================
-- Drop old chunks to bound storage growth.

-- Engagement snapshots: keep 1 year
SELECT add_retention_policy('engagement_snapshots', INTERVAL '365 days');

-- Activity logs: keep 90 days (summarized data lives in continuous aggregates)
SELECT add_retention_policy('activity_logs', INTERVAL '90 days');

-- Account health history: keep 180 days
SELECT add_retention_policy('account_health_history', INTERVAL '180 days');


-- =============================================================================
-- TIMESCALEDB: CONTINUOUS AGGREGATES
-- =============================================================================
-- Pre-computed rollups that update automatically as new data arrives.
-- These power dashboard queries without scanning raw hypertable data.

-- ---------------------------------------------------------------------------
-- Continuous Aggregate: Daily engagement summary per content per platform
-- ---------------------------------------------------------------------------
-- Powers: "content performance dashboard", "daily views report",
--         "content lifecycle classification"

CREATE MATERIALIZED VIEW daily_content_engagement
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', captured_at) AS bucket_day,
    content_id,
    platform,
    -- Take the latest snapshot's values for each day (most recent checkpoint)
    max(views) AS max_views,
    max(likes) AS max_likes,
    max(comments) AS max_comments,
    max(shares) AS max_shares,
    max(saves) AS max_saves,
    max(follows_gained) AS max_follows_gained,
    -- Averages for rate metrics
    avg(completion_rate) AS avg_completion_rate,
    avg(engagement_rate) AS avg_engagement_rate,
    avg(fyp_pct) AS avg_fyp_pct,
    -- Count of snapshots (for data quality)
    count(*) AS snapshot_count
FROM engagement_snapshots
GROUP BY bucket_day, content_id, platform
WITH NO DATA;

-- Refresh policy: update every hour, cover last 3 days, discard older refreshes
SELECT add_continuous_aggregate_policy('daily_content_engagement',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

-- ---------------------------------------------------------------------------
-- Continuous Aggregate: Daily account activity summary
-- ---------------------------------------------------------------------------
-- Powers: "account activity dashboard", "warming progress tracking",
--         "action budget monitoring"

CREATE MATERIALIZED VIEW daily_account_activity
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', performed_at) AS bucket_day,
    account_id,
    platform,
    action,
    count(*) AS action_count,
    count(*) FILTER (WHERE success = true) AS success_count,
    count(*) FILTER (WHERE success = false) AS failure_count,
    avg(duration_ms) AS avg_duration_ms,
    max(performed_at) AS last_action_at
FROM activity_logs
GROUP BY bucket_day, account_id, platform, action
WITH NO DATA;

SELECT add_continuous_aggregate_policy('daily_account_activity',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

-- ---------------------------------------------------------------------------
-- Continuous Aggregate: Weekly engagement summary per niche
-- ---------------------------------------------------------------------------
-- Powers: "niche performance comparison", "which niches are growing"

CREATE MATERIALIZED VIEW weekly_niche_engagement
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('7 days', captured_at) AS bucket_week,
    es.platform,
    -- We can't join in continuous aggregates, so we denormalize content_id
    -- and join with contents table at query time. Alternatively, store niche_id
    -- in engagement_snapshots. For now, we aggregate by content_id.
    content_id,
    sum(views) AS total_views,
    sum(likes) AS total_likes,
    sum(comments) AS total_comments,
    sum(shares) AS total_shares,
    avg(completion_rate) AS avg_completion_rate,
    avg(engagement_rate) AS avg_engagement_rate,
    count(DISTINCT distribution_id) AS distribution_count
FROM engagement_snapshots es
GROUP BY bucket_week, es.platform, content_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('weekly_niche_engagement',
    start_offset => INTERVAL '14 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '6 hours'
);


-- =============================================================================
-- VIEWS: Common Dashboard Queries
-- =============================================================================

-- ---------------------------------------------------------------------------
-- View: Active accounts by platform with health context
-- ---------------------------------------------------------------------------
-- Query: "Get all active accounts for platform X"

CREATE VIEW v_active_accounts AS
SELECT
    a.id,
    a.platform,
    a.username,
    a.display_name,
    n.display_name AS niche_name,
    a.status,
    a.health_score,
    a.followers,
    a.total_posts,
    a.posts_today,
    a.max_posts_per_day,
    a.last_posted_at,
    d.label AS device_label,
    p.label AS proxy_label,
    a.created_at
FROM accounts a
LEFT JOIN niches n ON a.niche_id = n.id
LEFT JOIN devices d ON a.device_id = d.id
LEFT JOIN proxies p ON a.proxy_id = p.id
WHERE a.status = 'active'
  AND a.deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- View: Accounts needing warming attention
-- ---------------------------------------------------------------------------
-- Query: "Get all accounts in warming phase that need attention"
-- "Need attention" = warming for >14 days, or stuck in early phase for >3 days

CREATE VIEW v_warming_accounts AS
SELECT
    a.id,
    a.platform,
    a.username,
    n.display_name AS niche_name,
    a.warming_phase,
    a.warming_started_at,
    now() - a.warming_started_at AS warming_duration,
    a.health_score,
    d.label AS device_label,
    d.status AS device_status,
    CASE
        WHEN now() - a.warming_started_at > INTERVAL '14 days' THEN 'overdue'
        WHEN a.warming_phase IN ('browsing', 'engaging')
            AND now() - a.warming_started_at > INTERVAL '5 days' THEN 'stuck'
        ELSE 'on_track'
    END AS attention_level
FROM accounts a
LEFT JOIN niches n ON a.niche_id = n.id
LEFT JOIN devices d ON a.device_id = d.id
WHERE a.status = 'warming'
  AND a.deleted_at IS NULL
ORDER BY
    CASE
        WHEN now() - a.warming_started_at > INTERVAL '14 days' THEN 1
        WHEN a.warming_phase IN ('browsing', 'engaging')
            AND now() - a.warming_started_at > INTERVAL '5 days' THEN 2
        ELSE 3
    END,
    a.warming_started_at ASC;

-- ---------------------------------------------------------------------------
-- View: Hook template leaderboard per niche
-- ---------------------------------------------------------------------------
-- Query: "Find top-performing hook templates in niche Z"

CREATE VIEW v_hook_leaderboard AS
SELECT
    ht.id,
    ht.name,
    ht.category,
    n.display_name AS niche_name,
    ht.template_text,
    ht.ts_alpha,
    ht.ts_beta,
    ht.win_rate,
    ht.total_trials,
    ht.total_successes,
    ht.avg_completion_rate,
    ht.avg_engagement_rate,
    ht.sample_size,
    ht.variant_group,
    -- Thompson Sampling expected value (mean of Beta distribution)
    ht.ts_alpha / (ht.ts_alpha + ht.ts_beta) AS ts_expected_value,
    -- Confidence: higher alpha+beta = more confident
    ht.ts_alpha + ht.ts_beta AS ts_confidence
FROM hook_templates ht
LEFT JOIN niches n ON ht.niche_id = n.id
WHERE ht.is_active = true
ORDER BY ht.win_rate DESC;

-- ---------------------------------------------------------------------------
-- View: Daily content production cost summary
-- ---------------------------------------------------------------------------
-- Query: "Daily content production cost summary"

CREATE VIEW v_daily_cost_summary AS
SELECT
    cr.recorded_date,
    n.display_name AS niche_name,
    cr.category,
    count(*) AS line_items,
    sum(cr.amount_usd) AS total_usd,
    avg(cr.amount_usd) AS avg_per_item_usd
FROM cost_records cr
LEFT JOIN niches n ON cr.niche_id = n.id
GROUP BY cr.recorded_date, n.display_name, cr.category
ORDER BY cr.recorded_date DESC, total_usd DESC;

-- ---------------------------------------------------------------------------
-- View: Content pipeline status
-- ---------------------------------------------------------------------------
-- Overview of content in each pipeline stage

CREATE VIEW v_content_pipeline AS
SELECT
    c.status,
    n.display_name AS niche_name,
    count(*) AS content_count,
    min(c.created_at) AS oldest_item,
    max(c.created_at) AS newest_item
FROM contents c
JOIN niches n ON c.niche_id = n.id
GROUP BY c.status, n.display_name
ORDER BY
    CASE c.status
        WHEN 'draft' THEN 1
        WHEN 'scripted' THEN 2
        WHEN 'producing' THEN 3
        WHEN 'rendered' THEN 4
        WHEN 'qa_failed' THEN 5
        WHEN 'qa_passed' THEN 6
        WHEN 'queued' THEN 7
        WHEN 'published' THEN 8
        WHEN 'recycling' THEN 9
        WHEN 'retired' THEN 10
    END;

-- ---------------------------------------------------------------------------
-- View: Device farm overview
-- ---------------------------------------------------------------------------

CREATE VIEW v_device_overview AS
SELECT
    d.id,
    d.label,
    d.model,
    d.status,
    d.last_heartbeat,
    now() - d.last_heartbeat AS since_heartbeat,
    d.current_accounts,
    d.max_accounts,
    p.label AS proxy_label,
    CASE
        WHEN d.last_heartbeat IS NULL THEN 'never_seen'
        WHEN now() - d.last_heartbeat > INTERVAL '5 minutes' THEN 'stale'
        ELSE 'healthy'
    END AS connection_health,
    -- Count of accounts assigned to this device
    (SELECT count(*) FROM accounts a
     WHERE a.device_id = d.id AND a.deleted_at IS NULL) AS assigned_accounts
FROM devices d
LEFT JOIN proxies p ON d.proxy_id = p.id
ORDER BY d.status, d.label;

-- ---------------------------------------------------------------------------
-- View: Distribution queue (upcoming posts)
-- ---------------------------------------------------------------------------

CREATE VIEW v_distribution_queue AS
SELECT
    dist.id AS distribution_id,
    dist.scheduled_at,
    dist.platform,
    dist.status,
    a.username AS account_username,
    a.health_score AS account_health,
    c.title AS content_title,
    n.display_name AS niche_name,
    dist.stagger_group,
    dist.stagger_order,
    a.posts_today,
    a.max_posts_per_day,
    a.last_posted_at,
    -- Time until post
    dist.scheduled_at - now() AS time_until_post
FROM distributions dist
JOIN accounts a ON dist.account_id = a.id
JOIN contents c ON dist.content_id = c.id
JOIN niches n ON c.niche_id = n.id
WHERE dist.status IN ('pending', 'scheduled')
  AND dist.scheduled_at > now()
ORDER BY dist.scheduled_at ASC;

-- ---------------------------------------------------------------------------
-- View: Content performance with lifecycle classification
-- ---------------------------------------------------------------------------
-- Query: "Get content performance over time for content_id Y"
-- Use this view for the content detail page, then join with
-- engagement_snapshots for the full time series.

CREATE VIEW v_content_performance AS
SELECT
    c.id AS content_id,
    c.title,
    c.status,
    c.lifecycle_class,
    c.overperformance_ratio,
    c.content_pillar,
    c.topic,
    n.display_name AS niche_name,
    ht.name AS hook_template_name,
    ht.category AS hook_category,
    c.duration_secs,
    c.production_cost_usd,
    c.created_at,
    -- Aggregate across all distributions
    count(DISTINCT dist.id) AS distribution_count,
    count(DISTINCT dist.platform) AS platform_count,
    -- Latest metrics across all platforms (sum of maximums per distribution)
    sum(latest.max_views) AS total_views,
    sum(latest.max_likes) AS total_likes,
    sum(latest.max_comments) AS total_comments,
    sum(latest.max_shares) AS total_shares,
    sum(latest.max_saves) AS total_saves
FROM contents c
JOIN niches n ON c.niche_id = n.id
LEFT JOIN hook_templates ht ON c.hook_template_id = ht.id
LEFT JOIN distributions dist ON dist.content_id = c.id
LEFT JOIN LATERAL (
    SELECT
        max(views) AS max_views,
        max(likes) AS max_likes,
        max(comments) AS max_comments,
        max(shares) AS max_shares,
        max(saves) AS max_saves
    FROM engagement_snapshots es
    WHERE es.distribution_id = dist.id
) latest ON true
GROUP BY c.id, c.title, c.status, c.lifecycle_class, c.overperformance_ratio,
         c.content_pillar, c.topic, n.display_name, ht.name, ht.category,
         c.duration_secs, c.production_cost_usd, c.created_at;


-- =============================================================================
-- HELPER FUNCTIONS
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Function: Thompson Sampling — draw from Beta distribution for hook selection
-- ---------------------------------------------------------------------------
-- Usage: SELECT id, thompson_sample(ts_alpha, ts_beta) AS score
--        FROM hook_templates WHERE niche_id = $1 AND is_active
--        ORDER BY score DESC LIMIT 1;
--
-- Uses the inverse CDF approximation since PostgreSQL doesn't have a native
-- Beta distribution sampler. For production, consider calling from application
-- code with proper Beta sampling libraries.

CREATE OR REPLACE FUNCTION thompson_sample(alpha NUMERIC, beta NUMERIC)
RETURNS NUMERIC AS $$
DECLARE
    u1 NUMERIC;
    u2 NUMERIC;
    x NUMERIC;
    y NUMERIC;
BEGIN
    -- Simplified: use the mean + jitter approximation
    -- For production: use proper Beta sampling in application code
    -- This is a rough approximation using the Beta mean + uniform noise
    -- scaled by the variance
    u1 := random();
    -- Beta mean = alpha / (alpha + beta)
    -- Beta variance = (alpha * beta) / ((alpha + beta)^2 * (alpha + beta + 1))
    x := alpha / (alpha + beta);
    y := sqrt((alpha * beta) / ((alpha + beta) * (alpha + beta) * (alpha + beta + 1)));
    RETURN greatest(0, least(1, x + (u1 - 0.5) * 2 * y * 3));
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ---------------------------------------------------------------------------
-- Function: Update hook template Thompson Sampling params after content
-- performance is measured
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION update_hook_ts_params(
    p_hook_template_id UUID,
    p_success BOOLEAN
) RETURNS void AS $$
BEGIN
    UPDATE hook_templates
    SET
        ts_alpha = CASE WHEN p_success THEN ts_alpha + 1 ELSE ts_alpha END,
        ts_beta  = CASE WHEN p_success THEN ts_beta  ELSE ts_beta + 1 END,
        win_rate = CASE WHEN p_success
                        THEN (ts_alpha + 1) / (ts_alpha + 1 + ts_beta)
                        ELSE ts_alpha / (ts_alpha + ts_beta + 1)
                   END,
        total_trials = total_trials + 1,
        total_successes = total_successes + CASE WHEN p_success THEN 1 ELSE 0 END,
        updated_at = now()
    WHERE id = p_hook_template_id;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Function: Classify content lifecycle based on view distribution over time
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION classify_content_lifecycle(p_content_id UUID)
RETURNS content_lifecycle AS $$
DECLARE
    views_1h   BIGINT;
    views_24h  BIGINT;
    views_7d   BIGINT;
    views_30d  BIGINT;
    v_pct_24h  NUMERIC;
    v_pct_7d   NUMERIC;
    velocity_late NUMERIC;
BEGIN
    -- Get views at each checkpoint (max across all distributions for this content)
    SELECT max(CASE WHEN checkpoint = 't_1h' THEN views END),
           max(CASE WHEN checkpoint = 't_24h' THEN views END),
           max(CASE WHEN checkpoint = 't_7d' THEN views END),
           max(CASE WHEN checkpoint = 't_30d' THEN views END)
    INTO views_1h, views_24h, views_7d, views_30d
    FROM engagement_snapshots
    WHERE content_id = p_content_id;

    -- Need at least 7d data to classify
    IF views_7d IS NULL OR views_7d = 0 THEN
        RETURN NULL;
    END IF;

    IF views_30d IS NULL OR views_30d = 0 THEN
        views_30d := views_7d;  -- Use 7d as proxy
    END IF;

    v_pct_24h := views_24h::NUMERIC / NULLIF(views_30d, 0);
    v_pct_7d  := (views_30d - views_7d)::NUMERIC / NULLIF(views_30d, 0);

    -- Flash: >80% of total views came in first 24h
    IF v_pct_24h > 0.80 THEN
        RETURN 'flash';
    END IF;

    -- Evergreen: >20% of views came after day 7
    IF v_pct_7d > 0.20 THEN
        RETURN 'evergreen';
    END IF;

    -- Resurging: check if late velocity exceeds early velocity
    -- (views gained in days 7-30 per day > views gained in days 1-7 per day)
    IF views_30d > views_7d THEN
        velocity_late := (views_30d - views_7d)::NUMERIC / 23.0;
        IF velocity_late > (views_7d::NUMERIC / 7.0) THEN
            RETURN 'resurging';
        END IF;
    END IF;

    -- Default: slow burn
    RETURN 'slow_burn';
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- TRIGGERS
-- =============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON niches
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON proxies
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON hook_templates
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON scripts
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON contents
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON distributions
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();


-- =============================================================================
-- COMMENTS: Encryption Notes
-- =============================================================================
-- The following columns MUST be encrypted at the application layer using
-- AES-256-GCM (or equivalent) before storage. The database stores ciphertext.
--
-- accounts.password          -- Platform login password
-- accounts.totp_secret       -- TOTP 2FA secret key
-- accounts.session_tokens    -- Platform session cookies/tokens
-- accounts.api_keys          -- Platform API keys
-- accounts.email             -- Account email address
-- accounts.phone             -- Account phone number
-- proxies.host               -- Proxy hostname/IP
-- proxies.username           -- Proxy auth username
-- proxies.password           -- Proxy auth password
--
-- Key management: Use a dedicated secrets manager (AWS KMS, HashiCorp Vault)
-- with per-column or per-row encryption keys. Never store encryption keys
-- in the database or application config files.

COMMENT ON COLUMN accounts.password IS 'ENCRYPTED: AES-256-GCM. Platform login password.';
COMMENT ON COLUMN accounts.totp_secret IS 'ENCRYPTED: AES-256-GCM. TOTP 2FA secret.';
COMMENT ON COLUMN accounts.session_tokens IS 'ENCRYPTED: AES-256-GCM. Platform session data.';
COMMENT ON COLUMN accounts.api_keys IS 'ENCRYPTED: AES-256-GCM. Platform API credentials.';
COMMENT ON COLUMN accounts.email IS 'ENCRYPTED: AES-256-GCM. Account email.';
COMMENT ON COLUMN accounts.phone IS 'ENCRYPTED: AES-256-GCM. Account phone number.';
COMMENT ON COLUMN proxies.host IS 'ENCRYPTED: AES-256-GCM. Proxy host address.';
COMMENT ON COLUMN proxies.username IS 'ENCRYPTED: AES-256-GCM. Proxy auth username.';
COMMENT ON COLUMN proxies.password IS 'ENCRYPTED: AES-256-GCM. Proxy auth password.';


-- =============================================================================
-- SAMPLE QUERIES (for reference / testing)
-- =============================================================================

-- Q1: Get all active accounts for platform 'tiktok'
-- SELECT * FROM v_active_accounts WHERE platform = 'tiktok';

-- Q2: Get content performance over time for a specific content
-- SELECT * FROM engagement_snapshots
-- WHERE content_id = '<uuid>'
-- ORDER BY captured_at ASC;

-- Q3: Find top-performing hook templates in a niche
-- SELECT * FROM v_hook_leaderboard
-- WHERE niche_name = 'Personal Finance'
-- ORDER BY win_rate DESC
-- LIMIT 10;

-- Q4: Get all warming accounts that need attention
-- SELECT * FROM v_warming_accounts WHERE attention_level IN ('overdue', 'stuck');

-- Q5: Daily content production cost summary
-- SELECT * FROM v_daily_cost_summary WHERE recorded_date = CURRENT_DATE;

-- Q6: Thompson Sampling hook selection for a niche
-- SELECT id, name, template_text, thompson_sample(ts_alpha, ts_beta) AS score
-- FROM hook_templates
-- WHERE niche_id = '<uuid>' AND is_active = true
-- ORDER BY score DESC
-- LIMIT 1;

-- Q7: Content lifecycle distribution
-- SELECT lifecycle_class, count(*), avg(overperformance_ratio)
-- FROM contents
-- WHERE lifecycle_class IS NOT NULL
-- GROUP BY lifecycle_class;

-- Q8: Account activity budget check (how many actions left today)
-- SELECT
--     a.username,
--     a.daily_likes_budget - coalesce(daa.likes_today, 0) AS likes_remaining,
--     a.daily_comments_budget - coalesce(daa.comments_today, 0) AS comments_remaining,
--     a.daily_follows_budget - coalesce(daa.follows_today, 0) AS follows_remaining
-- FROM accounts a
-- LEFT JOIN LATERAL (
--     SELECT
--         count(*) FILTER (WHERE action = 'like') AS likes_today,
--         count(*) FILTER (WHERE action = 'comment') AS comments_today,
--         count(*) FILTER (WHERE action = 'follow') AS follows_today
--     FROM activity_logs al
--     WHERE al.account_id = a.id
--       AND al.performed_at >= date_trunc('day', now())
--       AND al.success = true
-- ) daa ON true
-- WHERE a.status IN ('warming', 'active');
