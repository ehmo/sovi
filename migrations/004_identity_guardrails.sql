-- 004: Identity isolation guardrails — device-account binding + session tracking
-- Run: psql -d sovi -f migrations/004_identity_guardrails.sql

-- =============================================================================
-- DEVICE-ACCOUNT BINDINGS — permanent account↔device pinning
-- =============================================================================

CREATE TABLE IF NOT EXISTS device_account_bindings (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
    bound_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    unbound_at  TIMESTAMPTZ,           -- NULL = active binding
    reason      TEXT NOT NULL DEFAULT 'initial',  -- initial | device_failure | manual_transfer
    notes       TEXT
);

-- Only 1 active binding per account
CREATE UNIQUE INDEX IF NOT EXISTS idx_bindings_active_account
    ON device_account_bindings (account_id) WHERE unbound_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_bindings_device_active
    ON device_account_bindings (device_id) WHERE unbound_at IS NULL;

-- =============================================================================
-- SESSION LOG — per-session tracking for cooldown/cap enforcement
-- =============================================================================

CREATE TABLE IF NOT EXISTS session_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_id       UUID NOT NULL REFERENCES devices(id),
    account_id      UUID REFERENCES accounts(id),
    session_type    TEXT NOT NULL,       -- warming | creation | posting
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    proxy_id        UUID REFERENCES proxies(id),
    identity_checks JSONB DEFAULT '{}',
    outcome         TEXT                -- success | failed | aborted
);

CREATE INDEX IF NOT EXISTS idx_session_log_device_time
    ON session_log (device_id, started_at DESC);

-- =============================================================================
-- ALTER devices — track last session end time
-- =============================================================================

ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_session_ended_at TIMESTAMPTZ;

-- =============================================================================
-- PL/pgSQL HELPERS
-- =============================================================================

-- bind_account_to_device: create binding + set accounts.device_id FK
CREATE OR REPLACE FUNCTION bind_account_to_device(
    p_account_id UUID,
    p_device_id  UUID,
    p_reason     TEXT DEFAULT 'initial'
) RETURNS UUID AS $$
DECLARE
    v_binding_id UUID;
    v_existing   UUID;
BEGIN
    -- Check for existing active binding
    SELECT id INTO v_existing
    FROM device_account_bindings
    WHERE account_id = p_account_id AND unbound_at IS NULL;

    IF v_existing IS NOT NULL THEN
        RAISE EXCEPTION 'Account % already bound (binding %)', p_account_id, v_existing;
    END IF;

    INSERT INTO device_account_bindings (account_id, device_id, reason)
    VALUES (p_account_id, p_device_id, p_reason)
    RETURNING id INTO v_binding_id;

    UPDATE accounts SET device_id = p_device_id, updated_at = now()
    WHERE id = p_account_id;

    RETURN v_binding_id;
END;
$$ LANGUAGE plpgsql;

-- transfer_account_device: close old binding, create new one (emergency only)
CREATE OR REPLACE FUNCTION transfer_account_device(
    p_account_id  UUID,
    p_new_device  UUID,
    p_reason      TEXT DEFAULT 'device_failure'
) RETURNS UUID AS $$
DECLARE
    v_binding_id UUID;
BEGIN
    -- Close existing binding
    UPDATE device_account_bindings
    SET unbound_at = now()
    WHERE account_id = p_account_id AND unbound_at IS NULL;

    -- Create new binding
    INSERT INTO device_account_bindings (account_id, device_id, reason)
    VALUES (p_account_id, p_new_device, p_reason)
    RETURNING id INTO v_binding_id;

    UPDATE accounts SET device_id = p_new_device, updated_at = now()
    WHERE id = p_account_id;

    RETURN v_binding_id;
END;
$$ LANGUAGE plpgsql;
