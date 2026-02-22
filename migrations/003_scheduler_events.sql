-- 003: Scheduler, system events, and account warming columns
-- Run: psql -d sovi -f migrations/003_scheduler_events.sql

-- =============================================================================
-- ACCOUNT COLUMNS
-- =============================================================================

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_warmed_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_post_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- =============================================================================
-- SYSTEM EVENTS TABLE
-- =============================================================================

CREATE TABLE IF NOT EXISTS system_events (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    category    TEXT NOT NULL,          -- scheduler, account, device, auth, error
    severity    TEXT NOT NULL,          -- info, warning, error, critical
    event_type  TEXT NOT NULL,          -- warming_complete, login_failed, etc.
    device_id   UUID REFERENCES devices(id),
    account_id  UUID REFERENCES accounts(id),
    message     TEXT NOT NULL,
    context     JSONB DEFAULT '{}',
    resolved    BOOLEAN DEFAULT false,
    resolved_by TEXT,                   -- 'human', 'llm_agent', 'auto'
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_events_unresolved
    ON system_events (severity, timestamp DESC)
    WHERE resolved = false;

CREATE INDEX IF NOT EXISTS idx_events_device
    ON system_events (device_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_account
    ON system_events (account_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_type_time
    ON system_events (event_type, timestamp DESC);

-- =============================================================================
-- WARMING SCHEDULING INDEX
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_accounts_needs_warming
    ON accounts (last_warmed_at ASC NULLS FIRST)
    WHERE current_state IN ('created', 'warming_p1', 'warming_p2', 'warming_p3', 'active')
      AND platform IN ('tiktok', 'instagram')
      AND deleted_at IS NULL;

-- =============================================================================
-- SEED NICHES: motivation + true_crime
-- =============================================================================

INSERT INTO niches (name, slug, tier, status)
VALUES
    ('Motivation', 'motivation', '2', 'active'),
    ('True Crime', 'true_crime', '2', 'active')
ON CONFLICT (slug) DO NOTHING;
