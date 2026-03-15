-- 005: Device role system — seeder/warmer role assignments, rotation, task queue
-- Run: psql -d sovi -f migrations/005_device_roles.sql

-- =============================================================================
-- DEVICE ROLE ENUM
-- =============================================================================

DO $$ BEGIN
    CREATE TYPE device_role AS ENUM ('seeder', 'warmer', 'idle');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- =============================================================================
-- DEVICE ROLE ASSIGNMENTS — history table tracking role changes
-- =============================================================================

CREATE TABLE IF NOT EXISTS device_role_assignments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    role            device_role NOT NULL,
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    cooldown_until  TIMESTAMPTZ,
    rotation_id     UUID,
    notes           TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_role_active_device
    ON device_role_assignments (device_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_role_active_role
    ON device_role_assignments (role) WHERE ended_at IS NULL;

-- =============================================================================
-- ALTER devices — add role tracking columns
-- =============================================================================

ALTER TABLE devices ADD COLUMN IF NOT EXISTS current_role device_role DEFAULT 'idle';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS role_changed_at TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS seeder_cooldown_until TIMESTAMPTZ;

-- =============================================================================
-- ALTER accounts — add seeder/warmer provenance + distribution eligibility
-- =============================================================================

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS seeded_by_device_id UUID REFERENCES devices(id);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS seeded_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS warmer_device_id UUID REFERENCES devices(id);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS distribution_eligible_at TIMESTAMPTZ;

-- =============================================================================
-- ALTER email_accounts — add verification tracking
-- =============================================================================

ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'unverified';
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS login_url TEXT;

-- =============================================================================
-- SEEDER TASKS QUEUE — work items for seeder devices
-- =============================================================================

CREATE TABLE IF NOT EXISTS seeder_tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    persona_id      UUID NOT NULL REFERENCES personas(id),
    platform        platform_type NOT NULL,
    task_type       TEXT NOT NULL CHECK (task_type IN ('create_email', 'create_account')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'claimed', 'in_progress', 'completed', 'failed', 'cancelled')),
    claimed_by      UUID REFERENCES devices(id),
    claimed_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,
    error_message   TEXT,
    result_id       UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_seeder_tasks_pending
    ON seeder_tasks (status, created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_seeder_tasks_claimed
    ON seeder_tasks (claimed_by) WHERE status IN ('claimed', 'in_progress');

-- =============================================================================
-- ROLE ROTATION FUNCTION
-- =============================================================================

CREATE OR REPLACE FUNCTION rotate_device_roles(
    p_new_seeder_ids UUID[],
    p_rotation_id UUID DEFAULT uuid_generate_v4()
) RETURNS VOID AS $$
BEGIN
    -- End current seeder assignments (except those staying as seeder)
    UPDATE device_role_assignments
    SET ended_at = now()
    WHERE role = 'seeder' AND ended_at IS NULL
      AND device_id != ALL(p_new_seeder_ids);

    -- Set cooldown on demoted seeders
    UPDATE devices
    SET current_role = 'warmer',
        role_changed_at = now(),
        seeder_cooldown_until = now() + interval '30 minutes'
    WHERE current_role = 'seeder'
      AND id != ALL(p_new_seeder_ids);

    -- Create warmer assignments for demoted seeders
    INSERT INTO device_role_assignments (device_id, role, rotation_id, cooldown_until)
    SELECT id, 'warmer', p_rotation_id, now() + interval '30 minutes'
    FROM devices
    WHERE current_role = 'warmer'
      AND id NOT IN (SELECT device_id FROM device_role_assignments WHERE role = 'seeder' AND ended_at IS NULL);

    -- End current warmer assignments for promoted devices
    UPDATE device_role_assignments
    SET ended_at = now()
    WHERE role = 'warmer' AND ended_at IS NULL
      AND device_id = ANY(p_new_seeder_ids);

    -- Create seeder assignments
    INSERT INTO device_role_assignments (device_id, role, rotation_id)
    SELECT unnest(p_new_seeder_ids), 'seeder', p_rotation_id;

    -- Update denormalized current_role
    UPDATE devices SET current_role = 'seeder', role_changed_at = now()
    WHERE id = ANY(p_new_seeder_ids);
END;
$$ LANGUAGE plpgsql;
