-- Add unique index on hooks (hook_text, hook_category) to prevent duplicates
-- when seed_hooks is run multiple times.
CREATE UNIQUE INDEX IF NOT EXISTS idx_hooks_text_category
    ON hooks (hook_text, hook_category);
