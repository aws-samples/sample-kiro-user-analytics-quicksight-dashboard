-- Cohort retention curve, split by subscription tier.
-- Each user is grouped into a "cohort" by the MONTH they were first active,
-- and the tier they have in user_dim (most-recent across history). For each
-- (cohort_month, tier), we count how many of those users were still active
-- in month N. retention_rate = active_in_month_N / cohort_size.
--
-- Monthly (not weekly) cohorts: weekly cohorts need ~a year of history to
-- read well, and most Kiro deployments will have weeks-to-months of export
-- data. Monthly buckets stay legible from the first month of data.
--
-- Tier assignment is sticky to user_dim's per-user value rather than the
-- tier the user had in their cohort month. This keeps the cohort denominator
-- stable for tier-filtered views: a user who upgrades Pro -> Pro+ is counted
-- in Pro+'s cohort line, not Pro's. Standard cohort analysis would use the
-- tier at cohort time, but this dashboard uses lifetime tier elsewhere
-- (user_totals, engagement_segmentation) so we stay consistent.
--
-- Output is one row per (cohort_month, months_since_first_active, tier) -
-- perfect shape for a line chart with cohort_month as the color dimension
-- and tier filtered by the People-sheet picker.

CREATE OR REPLACE VIEW ${database}.cohort_retention AS
WITH first_seen AS (
    SELECT
        b.user_id,
        d.subscription_tier,
        date_trunc('month', MIN(b.activity_date)) AS cohort_month
    FROM ${database}.base_user_activity b
    LEFT JOIN ${database}.user_dim d ON d.user_id = b.user_id
    GROUP BY b.user_id, d.subscription_tier
),
monthly_activity AS (
    SELECT DISTINCT
        user_id,
        date_trunc('month', activity_date) AS active_month
    FROM ${database}.base_user_activity
),
cohort_sizes AS (
    SELECT cohort_month, subscription_tier, COUNT(*) AS cohort_size
    FROM first_seen
    GROUP BY cohort_month, subscription_tier
),
joined AS (
    SELECT
        f.cohort_month,
        f.subscription_tier,
        date_diff('month', f.cohort_month, m.active_month) AS months_since,
        COUNT(DISTINCT m.user_id) AS active_users
    FROM first_seen f
    JOIN monthly_activity m ON m.user_id = f.user_id
    WHERE m.active_month >= f.cohort_month
    GROUP BY f.cohort_month, f.subscription_tier, date_diff('month', f.cohort_month, m.active_month)
)
SELECT
    j.cohort_month,
    j.months_since,
    j.subscription_tier,
    j.active_users,
    c.cohort_size,
    CAST(j.active_users AS double) / c.cohort_size AS retention_rate
FROM joined j
JOIN cohort_sizes c
    ON c.cohort_month       = j.cohort_month
   AND c.subscription_tier IS NOT DISTINCT FROM j.subscription_tier;
