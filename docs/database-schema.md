# Database Schema

## Connection

- **Host**: `localhost:5432` (on studio)
- **Database**: `sovi`
- **App user**: `sovi` (password: `sovi`)
- **Table owner**: `noh` (system user — for DDL operations, connect as `noh`)
- **Driver**: `psycopg` 3.x with `dict_row` factory
- **Pool**: `psycopg_pool.AsyncConnectionPool` (min 2, max 10)

## Migrations

| Migration | Description |
|-----------|-------------|
| `001_initial_schema.sql` | Full initial schema: niches, devices, accounts, hooks, content, distributions, activity_log (partitioned), warming_progress, metric_snapshots (hypertable) |
| `002_continuous_aggregates.sql` | TimescaleDB continuous aggregates: hourly/daily metric rollups, account health daily |
| `003_scheduler_events.sql` | system_events table, account warming columns (last_warmed_at, last_post_at, deleted_at), warming scheduling index, motivation + true_crime niches |

**Note:** Migration 002 requires TimescaleDB extension which is not currently installed on studio's native PostgreSQL 17. These views are aspirational.

## Core Tables

### niches

Content vertical definitions.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | Auto-generated |
| name | TEXT | Display name |
| slug | TEXT UNIQUE | URL-safe identifier |
| tier | TEXT | "1", "2", etc. |
| status | TEXT | "active", "inactive" |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

**Seed data:** personal_finance, ai_storytelling, tech_ai_tools, motivation, true_crime

### devices

Physical iOS device fleet.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | Auto-generated |
| name | TEXT | Human name (iPhone-A) |
| model | TEXT | iPhone model |
| udid | TEXT UNIQUE | Device UDID |
| ios_version | TEXT | iOS version string |
| wda_port | INTEGER | iproxy local port |
| status | TEXT | active/maintenance/failed/disconnected |
| connected_since | TIMESTAMPTZ | Last connection time |
| battery_level | FLOAT | 0-100 |
| storage_free_gb | FLOAT | Available storage |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### accounts

Managed social media accounts.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | Auto-generated |
| platform | TEXT | tiktok, instagram |
| username | TEXT | Platform username |
| email_enc | BYTEA | AES-256-GCM encrypted email |
| password_enc | BYTEA | AES-256-GCM encrypted password |
| totp_secret_enc | BYTEA | AES-256-GCM encrypted TOTP secret |
| proxy_credentials | TEXT | Encrypted proxy auth |
| niche_id | UUID FK → niches | Content vertical |
| device_id | UUID FK → devices | Last used device |
| current_state | TEXT | Account state enum |
| warming_day_count | INTEGER | Days warmed |
| followers | INTEGER | Current follower count |
| following | INTEGER | Current following count |
| bio | TEXT | Profile bio |
| profile_pic_url | TEXT | |
| last_activity_at | TIMESTAMPTZ | |
| last_warmed_at | TIMESTAMPTZ | Last warming session |
| last_post_at | TIMESTAMPTZ | Last content post |
| deleted_at | TIMESTAMPTZ | Soft delete timestamp |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

**Account States:**
```
created → warming_p1 → warming_p2 → warming_p3 → active → resting
                                                        → cooldown
                                                        → flagged
                                                        → restricted
                                                        → shadowbanned
                                                        → suspended
                                                        → banned
```

**Key Index:**
```sql
idx_accounts_needs_warming ON accounts (last_warmed_at ASC NULLS FIRST)
WHERE current_state IN ('created', 'warming_p1', 'warming_p2', 'warming_p3', 'active')
  AND platform IN ('tiktok', 'instagram')
  AND deleted_at IS NULL;
```

### system_events

Structured event log for all subsystems.

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL PK | Auto-increment |
| timestamp | TIMESTAMPTZ | Event time (default now()) |
| category | TEXT | scheduler, account, device, auth |
| severity | TEXT | info, warning, error, critical |
| event_type | TEXT | warming_complete, login_failed, etc. |
| device_id | UUID FK → devices | Optional |
| account_id | UUID FK → accounts | Optional |
| message | TEXT | Human-readable message |
| context | JSONB | Structured metadata |
| resolved | BOOLEAN | Default false |
| resolved_by | TEXT | human, llm_agent, auto |
| resolved_at | TIMESTAMPTZ | |

**Indexes:**
- `idx_events_unresolved` — severity + timestamp WHERE resolved = false
- `idx_events_device` — device_id + timestamp
- `idx_events_account` — account_id + timestamp
- `idx_events_type_time` — event_type + timestamp

### hooks

Content hook templates for video openings.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | |
| category | TEXT | curiosity_gap, bold_claim, etc. |
| template | TEXT | Hook text template |
| effectiveness_score | FLOAT | 0.0-1.0 |
| usage_count | INTEGER | |
| platform_scores | JSONB | Per-platform performance |
| niche_id | UUID FK → niches | |

### content

Video content through the production pipeline.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | |
| topic | TEXT | Video topic |
| niche_id | UUID FK | |
| content_format | TEXT | faceless, reddit_story, etc. |
| production_status | TEXT | scripting→generating→assembling→distributing→complete/failed |
| script_json | JSONB | Generated script |
| asset_manifest | JSONB | Asset URLs and metadata |
| quality_score | FLOAT | 0.0-1.0 |
| hook_id | UUID FK → hooks | |
| output_path | TEXT | Final video file path |
| created_at | TIMESTAMPTZ | |

### distributions

Content posted to platforms.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | |
| content_id | UUID FK → content | |
| account_id | UUID FK → accounts | |
| platform | TEXT | |
| posted_at | TIMESTAMPTZ | |
| platform_post_id | TEXT | Platform's ID for the post |
| caption | TEXT | |
| hashtags | TEXT[] | |
| status | TEXT | pending, posted, failed, removed |

### trending_topics

Research-discovered trending topics.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | |
| niche_id | UUID FK | |
| topic | TEXT | |
| source | TEXT | reddit, tiktok |
| source_url | TEXT | |
| trend_score | FLOAT | |
| overperformance_ratio | FLOAT | |
| is_active | BOOLEAN | |
| discovered_at | TIMESTAMPTZ | |

### activity_log (Partitioned)

Low-level activity tracking, partitioned by month.

| Column | Type | Description |
|--------|------|-------------|
| id | BIGSERIAL | |
| timestamp | TIMESTAMPTZ | Partition key |
| device_id | UUID | |
| account_id | UUID | |
| action_type | TEXT | |
| detail_json | JSONB | |

### metric_snapshots (TimescaleDB Hypertable)

Time-series engagement metrics.

| Column | Type | Description |
|--------|------|-------------|
| time | TIMESTAMPTZ | Hypertable time column |
| distribution_id | UUID FK | |
| account_id | UUID FK | |
| views | INTEGER | |
| likes | INTEGER | |
| comments | INTEGER | |
| shares | INTEGER | |
| saves | INTEGER | |
| completion_rate | FLOAT | |
| engagement_rate | FLOAT | |
| follower_count_at | INTEGER | |

### warming_progress

Warming session history.

| Column | Type | Description |
|--------|------|-------------|
| id | UUID PK | |
| account_id | UUID FK | |
| device_id | UUID FK | |
| platform | TEXT | |
| warming_phase | INTEGER | 1-4 |
| warming_day | INTEGER | |
| session_data | JSONB | videos_watched, likes, follows, etc. |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | |
