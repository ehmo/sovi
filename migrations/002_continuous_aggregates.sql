-- SOVI Continuous Aggregates — requires tables from 001 to be committed first
-- Run: psql -d sovi -f migrations/002_continuous_aggregates.sql

-- =============================================================================
-- METRIC SNAPSHOTS — HOURLY ROLLUP
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS metric_snapshots_hourly CASCADE;
CREATE MATERIALIZED VIEW metric_snapshots_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', "time")   AS bucket,
    distribution_id,
    last(views, "time")             AS views,
    last(likes, "time")             AS likes,
    last(comments, "time")          AS comments,
    last(shares, "time")            AS shares,
    last(saves, "time")             AS saves,
    avg(completion_rate)            AS avg_completion_rate,
    avg(engagement_rate)            AS avg_engagement_rate,
    last(follower_count_at, "time") AS follower_count_at,
    max(views) - min(views)         AS views_delta,
    max(likes) - min(likes)         AS likes_delta
FROM metric_snapshots
GROUP BY bucket, distribution_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metric_snapshots_hourly',
    start_offset    => INTERVAL '3 hours',
    end_offset      => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists   => TRUE
);

-- =============================================================================
-- METRIC SNAPSHOTS — DAILY ROLLUP
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS metric_snapshots_daily CASCADE;
CREATE MATERIALIZED VIEW metric_snapshots_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', "time")    AS bucket,
    distribution_id,
    last(views, "time")             AS views,
    last(likes, "time")             AS likes,
    last(comments, "time")          AS comments,
    last(shares, "time")            AS shares,
    last(saves, "time")             AS saves,
    avg(completion_rate)            AS avg_completion_rate,
    avg(engagement_rate)            AS avg_engagement_rate,
    last(follower_count_at, "time") AS follower_count_at,
    max(views) - min(views)         AS views_delta,
    max(likes) - min(likes)         AS likes_delta,
    max(comments) - min(comments)   AS comments_delta,
    max(shares) - min(shares)       AS shares_delta
FROM metric_snapshots
GROUP BY bucket, distribution_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metric_snapshots_daily',
    start_offset    => INTERVAL '3 days',
    end_offset      => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists   => TRUE
);

-- =============================================================================
-- ACCOUNT HEALTH — DAILY ROLLUP
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS account_health_daily CASCADE;
CREATE MATERIALIZED VIEW account_health_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', "time")        AS bucket,
    account_id,
    avg(reach_rate)                     AS avg_reach_rate,
    avg(engagement_rate)                AS avg_engagement_rate,
    avg(avg_completion_rate)            AS avg_completion_rate,
    last(growth_rate_7d, "time")        AS growth_rate_7d,
    last(followers, "time")             AS followers,
    last(following, "time")             AS following,
    bool_or(is_shadowbanned)            AS was_shadowbanned,
    max(action_blocks_24h)              AS max_action_blocks,
    max(content_removals_7d)            AS max_content_removals
FROM account_health_snapshots
GROUP BY bucket, account_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('account_health_daily',
    start_offset    => INTERVAL '3 days',
    end_offset      => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists   => TRUE
);
