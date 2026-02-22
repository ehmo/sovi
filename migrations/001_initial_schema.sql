-- SOVI Initial Schema — PostgreSQL 17 + TimescaleDB 2.25
-- Run: psql -d sovi -f migrations/001_initial_schema.sql

-- =============================================================================
-- EXTENSIONS (already created, but idempotent)
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "timescaledb";

-- =============================================================================
-- ENUM TYPES
-- =============================================================================
DO $$ BEGIN
    CREATE TYPE platform_type AS ENUM (
        'tiktok', 'instagram', 'youtube_shorts', 'reddit', 'x_twitter'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE account_state AS ENUM (
        'created',
        'warming_p1', 'warming_p2', 'warming_p3',
        'active', 'resting', 'cooldown',
        'flagged', 'restricted', 'suspended', 'banned', 'shadowbanned'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE device_status AS ENUM (
        'active', 'maintenance', 'failed', 'disconnected'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE proxy_type AS ENUM (
        'mobile', 'residential', 'isp'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE niche_tier AS ENUM ('1', '2', '3');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE niche_status AS ENUM ('active', 'paused');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE content_format AS ENUM (
        'faceless', 'reddit_story', 'ai_avatar', 'carousel', 'meme', 'listicle'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE production_status AS ENUM (
        'scripting', 'generating', 'assembling', 'distributing', 'complete', 'failed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE asset_type AS ENUM (
        'voiceover', 'image', 'video_clip', 'music', 'caption_file'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE asset_model AS ENUM (
        'kling_3_std', 'kling_3_pro', 'hailuo_02', 'hunyuan', 'flux2',
        'elevenlabs', 'openai_tts', 'suno', 'deepgram', 'stock',
        'seedance', 'pika', 'runway', 'sora', 'luma'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE distribution_status AS ENUM (
        'queued', 'posting', 'posted', 'failed', 'removed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE posting_method AS ENUM (
        'late_api', 'upload_post', 'native_device'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE activity_type AS ENUM (
        'watch', 'like', 'comment', 'follow', 'unfollow',
        'post', 'share', 'save', 'search', 'scroll'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE warming_phase AS ENUM ('p1', 'p2', 'p3');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hook_category AS ENUM (
        'curiosity_gap', 'bold_claim', 'problem_pain', 'proof_results',
        'numbers_data', 'urgency_fomo', 'list_structure', 'personal_story',
        'shock_tension', 'direct_callout'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE flag_type AS ENUM (
        'copyright', 'policy', 'spam', 'shadowban'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =============================================================================
-- CORE TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS niches (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    config_yaml     TEXT,
    tier            niche_tier NOT NULL DEFAULT '2',
    status          niche_status NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS proxies (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider            TEXT NOT NULL,
    type                proxy_type NOT NULL,
    host                TEXT NOT NULL,
    port                INT NOT NULL CHECK (port BETWEEN 1 AND 65535),
    credentials_enc     BYTEA,
    geo_country         TEXT,
    geo_region          TEXT,
    geo_city            TEXT,
    assigned_device_id  UUID,
    ip_reputation_score NUMERIC(4,2) CHECK (ip_reputation_score BETWEEN 0 AND 10),
    last_health_check   TIMESTAMPTZ,
    is_healthy          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (host, port)
);

CREATE TABLE IF NOT EXISTS devices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT,
    model           TEXT NOT NULL,
    udid            TEXT NOT NULL UNIQUE,
    ios_version     TEXT NOT NULL,
    wda_port        INT CHECK (wda_port BETWEEN 1 AND 65535),
    current_proxy_id UUID REFERENCES proxies(id) ON DELETE SET NULL,
    status          device_status NOT NULL DEFAULT 'disconnected',
    connected_since TIMESTAMPTZ,
    battery_level   SMALLINT CHECK (battery_level BETWEEN 0 AND 100),
    storage_free_gb NUMERIC(6,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE proxies DROP CONSTRAINT IF EXISTS fk_proxies_device;
ALTER TABLE proxies
    ADD CONSTRAINT fk_proxies_device
    FOREIGN KEY (assigned_device_id) REFERENCES devices(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS accounts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    platform            platform_type NOT NULL,
    username            TEXT NOT NULL,
    display_name        TEXT,
    email_enc           BYTEA,
    password_enc        BYTEA,
    phone_number_enc    BYTEA,
    totp_secret_enc     BYTEA,
    proxy_id            UUID REFERENCES proxies(id) ON DELETE SET NULL,
    device_id           UUID REFERENCES devices(id) ON DELETE SET NULL,
    niche_id            UUID REFERENCES niches(id) ON DELETE SET NULL,
    current_state       account_state NOT NULL DEFAULT 'created',
    warming_day_count   INT NOT NULL DEFAULT 0 CHECK (warming_day_count >= 0),
    followers           INT NOT NULL DEFAULT 0 CHECK (followers >= 0),
    following           INT NOT NULL DEFAULT 0 CHECK (following >= 0),
    is_shadowbanned     BOOLEAN NOT NULL DEFAULT FALSE,
    bio_text            TEXT,
    profile_pic_url     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at    TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, username)
);

-- =============================================================================
-- CONTENT PRODUCTION TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS hooks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hook_text           TEXT NOT NULL,
    template_text       TEXT,
    hook_category       hook_category NOT NULL,
    emotional_tone      TEXT,
    platform            platform_type,
    niche_id            UUID REFERENCES niches(id) ON DELETE SET NULL,
    times_used          INT NOT NULL DEFAULT 0 CHECK (times_used >= 0),
    avg_3s_retention    NUMERIC(5,4),
    avg_engagement_rate NUMERIC(5,4),
    performance_score   NUMERIC(6,3),
    thompson_alpha      NUMERIC(10,4) NOT NULL DEFAULT 1.0,
    thompson_beta       NUMERIC(10,4) NOT NULL DEFAULT 1.0,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS content (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    niche_id            UUID NOT NULL REFERENCES niches(id) ON DELETE RESTRICT,
    topic               TEXT NOT NULL,
    script_text         TEXT,
    hook_id             UUID REFERENCES hooks(id) ON DELETE SET NULL,
    content_format      content_format NOT NULL,
    production_status   production_status NOT NULL DEFAULT 'scripting',
    quality_score       NUMERIC(4,2) CHECK (quality_score BETWEEN 0 AND 10),
    cost_usd            NUMERIC(8,4) NOT NULL DEFAULT 0,
    duration_seconds    NUMERIC(6,2),
    file_paths          JSONB NOT NULL DEFAULT '{}',
    temporal_workflow_id TEXT,
    source_reddit_url   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS content_assets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_id      UUID NOT NULL REFERENCES content(id) ON DELETE CASCADE,
    asset_type      asset_type NOT NULL,
    model_used      asset_model NOT NULL,
    cost_usd        NUMERIC(8,4) NOT NULL DEFAULT 0,
    file_path       TEXT NOT NULL,
    duration_seconds NUMERIC(6,2),
    metadata_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- DISTRIBUTION TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS distributions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_id          UUID NOT NULL REFERENCES content(id) ON DELETE CASCADE,
    account_id          UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform            platform_type NOT NULL,
    status              distribution_status NOT NULL DEFAULT 'queued',
    posted_at           TIMESTAMPTZ,
    post_url            TEXT,
    post_id_on_platform TEXT,
    caption_text        TEXT,
    hashtags            TEXT[],
    scheduled_for       TIMESTAMPTZ,
    posted_via          posting_method,
    error_message       TEXT,
    retry_count         SMALLINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Activity log — range-partitioned by timestamp
CREATE TABLE IF NOT EXISTS activity_log (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL,
    device_id       UUID,
    activity_type   activity_type NOT NULL,
    detail_json     JSONB,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    error_message   TEXT,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (timestamp);

-- Create monthly partitions for 2026
CREATE TABLE IF NOT EXISTS activity_log_2026_02 PARTITION OF activity_log
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_03 PARTITION OF activity_log
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_04 PARTITION OF activity_log
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_05 PARTITION OF activity_log
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_06 PARTITION OF activity_log
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_07 PARTITION OF activity_log
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_08 PARTITION OF activity_log
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_09 PARTITION OF activity_log
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_10 PARTITION OF activity_log
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_11 PARTITION OF activity_log
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE IF NOT EXISTS activity_log_2026_12 PARTITION OF activity_log
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- Warming progress
CREATE TABLE IF NOT EXISTS warming_progress (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    phase           warming_phase NOT NULL,
    day_in_phase    INT NOT NULL CHECK (day_in_phase >= 1),
    likes_given     INT NOT NULL DEFAULT 0,
    comments_made   INT NOT NULL DEFAULT 0,
    posts_created   INT NOT NULL DEFAULT 0,
    views_received  INT NOT NULL DEFAULT 0,
    follows_given   INT NOT NULL DEFAULT 0,
    metrics_json    JSONB,
    graduated_at    TIMESTAMPTZ,
    recorded_at     DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, phase, day_in_phase)
);

-- =============================================================================
-- TIMESCALEDB HYPERTABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS metric_snapshots (
    time                TIMESTAMPTZ NOT NULL,
    distribution_id     UUID NOT NULL,
    views               INT NOT NULL DEFAULT 0,
    likes               INT NOT NULL DEFAULT 0,
    comments            INT NOT NULL DEFAULT 0,
    shares              INT NOT NULL DEFAULT 0,
    saves               INT NOT NULL DEFAULT 0,
    completion_rate     NUMERIC(5,4),
    engagement_rate     NUMERIC(5,4),
    follower_count_at   INT,
    source_breakdown    JSONB
);

SELECT create_hypertable('metric_snapshots', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS account_health_snapshots (
    time                    TIMESTAMPTZ NOT NULL,
    account_id              UUID NOT NULL,
    reach_rate              NUMERIC(5,4),
    engagement_rate         NUMERIC(5,4),
    avg_completion_rate     NUMERIC(5,4),
    growth_rate_7d          NUMERIC(8,4),
    followers               INT NOT NULL DEFAULT 0,
    following               INT NOT NULL DEFAULT 0,
    is_shadowbanned         BOOLEAN NOT NULL DEFAULT FALSE,
    action_blocks_24h       INT NOT NULL DEFAULT 0,
    content_removals_7d     INT NOT NULL DEFAULT 0
);

SELECT create_hypertable('account_health_snapshots', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- =============================================================================
-- SUPPORTING TABLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS trending_topics (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    platform        platform_type NOT NULL,
    topic_text      TEXT,
    hashtag         TEXT,
    sound_id        TEXT,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    peak_at         TIMESTAMPTZ,
    trend_score     NUMERIC(12,2),
    niche_id        UUID REFERENCES niches(id) ON DELETE SET NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS content_flags (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_id      UUID REFERENCES content(id) ON DELETE CASCADE,
    distribution_id UUID REFERENCES distributions(id) ON DELETE CASCADE,
    flag_type       flag_type NOT NULL,
    platform        platform_type,
    detail_text     TEXT,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    flagged_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    CHECK (content_id IS NOT NULL OR distribution_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS niche_subreddit_map (
    niche_id                UUID NOT NULL REFERENCES niches(id) ON DELETE CASCADE,
    subreddit_name          TEXT NOT NULL,
    min_karma_required      INT NOT NULL DEFAULT 0,
    min_account_age_days    INT NOT NULL DEFAULT 0,
    posting_frequency_limit INTERVAL,
    automod_rules_json      JSONB,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (niche_id, subreddit_name)
);

-- =============================================================================
-- INDEXES
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_accounts_platform_state
    ON accounts (platform, current_state);
CREATE INDEX IF NOT EXISTS idx_accounts_niche
    ON accounts (niche_id) WHERE niche_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_accounts_device
    ON accounts (device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_accounts_state_activity
    ON accounts (current_state, last_activity_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_niche_created
    ON content (niche_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_format_status
    ON content (content_format, production_status);
CREATE INDEX IF NOT EXISTS idx_content_temporal_workflow
    ON content (temporal_workflow_id) WHERE temporal_workflow_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_content_quality
    ON content (quality_score DESC NULLS LAST)
    WHERE production_status = 'complete';

CREATE INDEX IF NOT EXISTS idx_distributions_account_date
    ON distributions (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_distributions_content
    ON distributions (content_id);
CREATE INDEX IF NOT EXISTS idx_distributions_status
    ON distributions (status)
    WHERE status IN ('queued', 'posting');
CREATE INDEX IF NOT EXISTS idx_distributions_platform_posted
    ON distributions (platform, posted_at DESC)
    WHERE status = 'posted';
CREATE INDEX IF NOT EXISTS idx_distributions_scheduled
    ON distributions (scheduled_for)
    WHERE status = 'queued' AND scheduled_for IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_metrics_distribution_time
    ON metric_snapshots (distribution_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_health_account_time
    ON account_health_snapshots (account_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_activity_account_time
    ON activity_log (account_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type_time
    ON activity_log (activity_type, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_hooks_category_score
    ON hooks (hook_category, performance_score DESC NULLS LAST)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_hooks_niche_platform
    ON hooks (niche_id, platform)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_hooks_thompson
    ON hooks (thompson_alpha, thompson_beta)
    WHERE is_active = TRUE AND times_used > 0;

CREATE INDEX IF NOT EXISTS idx_trending_platform_active
    ON trending_topics (platform, trend_score DESC)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_flags_unresolved
    ON content_flags (flag_type, flagged_at DESC)
    WHERE resolved = FALSE;
CREATE INDEX IF NOT EXISTS idx_warming_account_phase
    ON warming_progress (account_id, phase, day_in_phase);

-- =============================================================================
-- TIMESCALEDB CONTINUOUS AGGREGATES
-- =============================================================================

-- Note: continuous aggregates don't support IF NOT EXISTS; drop first on re-run
DROP MATERIALIZED VIEW IF EXISTS metric_snapshots_hourly CASCADE;
CREATE MATERIALIZED VIEW metric_snapshots_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time)     AS bucket,
    distribution_id,
    last(views, time)               AS views,
    last(likes, time)               AS likes,
    last(comments, time)            AS comments,
    last(shares, time)              AS shares,
    last(saves, time)               AS saves,
    avg(completion_rate)            AS avg_completion_rate,
    avg(engagement_rate)            AS avg_engagement_rate,
    last(follower_count_at, time)   AS follower_count_at,
    max(views) - min(views)         AS views_delta,
    max(likes) - min(likes)         AS likes_delta
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metric_snapshots_hourly',
    start_offset    => INTERVAL '3 hours',
    end_offset      => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists   => TRUE
);

DROP MATERIALIZED VIEW IF EXISTS metric_snapshots_daily CASCADE;
CREATE MATERIALIZED VIEW metric_snapshots_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time)      AS bucket,
    distribution_id,
    last(views, time)               AS views,
    last(likes, time)               AS likes,
    last(comments, time)            AS comments,
    last(shares, time)              AS shares,
    last(saves, time)               AS saves,
    avg(completion_rate)            AS avg_completion_rate,
    avg(engagement_rate)            AS avg_engagement_rate,
    last(follower_count_at, time)   AS follower_count_at,
    max(views) - min(views)         AS views_delta,
    max(likes) - min(likes)         AS likes_delta,
    max(comments) - min(comments)   AS comments_delta,
    max(shares) - min(shares)       AS shares_delta
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metric_snapshots_daily',
    start_offset    => INTERVAL '3 days',
    end_offset      => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists   => TRUE
);

DROP MATERIALIZED VIEW IF EXISTS account_health_daily CASCADE;
CREATE MATERIALIZED VIEW account_health_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time)          AS bucket,
    account_id,
    avg(reach_rate)                     AS avg_reach_rate,
    avg(engagement_rate)                AS avg_engagement_rate,
    avg(avg_completion_rate)            AS avg_completion_rate,
    last(growth_rate_7d, time)          AS growth_rate_7d,
    last(followers, time)               AS followers,
    last(following, time)               AS following,
    bool_or(is_shadowbanned)            AS was_shadowbanned,
    max(action_blocks_24h)              AS max_action_blocks,
    max(content_removals_7d)            AS max_content_removals
WITH NO DATA;

SELECT add_continuous_aggregate_policy('account_health_daily',
    start_offset    => INTERVAL '3 days',
    end_offset      => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists   => TRUE
);

-- =============================================================================
-- RETENTION & COMPRESSION POLICIES
-- =============================================================================

SELECT add_retention_policy('metric_snapshots', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('account_health_snapshots', INTERVAL '90 days', if_not_exists => TRUE);

ALTER TABLE metric_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'distribution_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('metric_snapshots', INTERVAL '7 days', if_not_exists => TRUE);

ALTER TABLE account_health_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'account_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('account_health_snapshots', INTERVAL '7 days', if_not_exists => TRUE);

-- =============================================================================
-- SEED DATA: devices and initial niche
-- =============================================================================

INSERT INTO devices (name, model, udid, ios_version, wda_port, status, connected_since)
VALUES
    ('iPhone-A', 'iPhone 16', '00008140-001975DC3678801C', '18.3', 8100, 'active', now()),
    ('iPhone-B', 'iPhone 16', '00008140-001A00141163001C', '18.3', 8101, 'active', now())
ON CONFLICT (udid) DO NOTHING;

INSERT INTO niches (name, slug, tier, status)
VALUES
    ('Personal Finance', 'personal_finance', '1', 'active'),
    ('AI Storytelling', 'ai_storytelling', '1', 'active'),
    ('Tech & AI Tools', 'tech_ai_tools', '1', 'active')
ON CONFLICT (slug) DO NOTHING;
