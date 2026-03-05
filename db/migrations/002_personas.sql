-- =============================================================================
-- Migration 002: Persona-first account creation pipeline
-- =============================================================================
-- Adds personas, persona_photos, email_accounts tables and links accounts
-- to personas. Also extends platform_type enum with facebook and linkedin.

BEGIN;

-- ---------------------------------------------------------------------------
-- Extend platform_type enum
-- ---------------------------------------------------------------------------
ALTER TYPE platform_type ADD VALUE IF NOT EXISTS 'facebook';
ALTER TYPE platform_type ADD VALUE IF NOT EXISTS 'linkedin';

COMMIT;

-- Enum additions must be in their own transaction before use
BEGIN;

-- ---------------------------------------------------------------------------
-- TABLE: personas
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personas (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    niche_id        UUID NOT NULL REFERENCES niches(id) ON DELETE RESTRICT,

    -- Identity
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    username_base   TEXT NOT NULL,

    -- Demographics
    gender          TEXT NOT NULL,
    date_of_birth   DATE NOT NULL,
    age             SMALLINT NOT NULL,
    country         TEXT NOT NULL DEFAULT 'US',
    state           TEXT,
    city            TEXT,

    -- Profile
    occupation      TEXT,
    bio_short       TEXT NOT NULL,
    bio_long        TEXT,
    interests       TEXT[] NOT NULL DEFAULT '{}',
    personality     JSONB DEFAULT '{}'::jsonb,

    -- Photo generation
    face_seed       TEXT,
    photo_style     TEXT DEFAULT 'realistic',
    photos_generated BOOLEAN DEFAULT false,

    -- Status
    status          TEXT NOT NULL DEFAULT 'draft',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_personas_niche ON personas (niche_id);
CREATE INDEX IF NOT EXISTS idx_personas_status ON personas (status);

-- ---------------------------------------------------------------------------
-- TABLE: persona_photos
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persona_photos (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    persona_id      UUID NOT NULL REFERENCES personas(id) ON DELETE CASCADE,

    file_path       TEXT NOT NULL,
    photo_type      TEXT NOT NULL,
    prompt_used     TEXT,
    width           INT,
    height          INT,
    is_primary      BOOLEAN DEFAULT false,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_persona_photos_persona ON persona_photos (persona_id);

-- ---------------------------------------------------------------------------
-- TABLE: email_accounts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_accounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    persona_id      UUID REFERENCES personas(id) ON DELETE SET NULL,

    provider        TEXT NOT NULL,
    email           TEXT NOT NULL,       -- ENCRYPTED
    password        TEXT NOT NULL,       -- ENCRYPTED
    imap_host       TEXT NOT NULL,
    imap_port       INT NOT NULL DEFAULT 993,
    domain          TEXT NOT NULL,

    status          TEXT NOT NULL DEFAULT 'available',
    phone_used      BOOLEAN DEFAULT false,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_email_accounts_status ON email_accounts (status) WHERE status = 'available';
CREATE INDEX IF NOT EXISTS idx_email_accounts_persona ON email_accounts (persona_id);

-- ---------------------------------------------------------------------------
-- ALTER accounts: add persona_id and email_account_id FKs
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'accounts' AND column_name = 'persona_id'
    ) THEN
        ALTER TABLE accounts ADD COLUMN persona_id UUID REFERENCES personas(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'accounts' AND column_name = 'email_account_id'
    ) THEN
        ALTER TABLE accounts ADD COLUMN email_account_id UUID REFERENCES email_accounts(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_accounts_persona ON accounts (persona_id);

COMMIT;
