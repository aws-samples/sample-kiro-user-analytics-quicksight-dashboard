-- Period comparison: current 30d vs prior 30d, broken down by tier.
-- Anchored on MAX(activity_date) so the windows track the latest export.
-- Output is one row per (period, subscription_tier) with messages + credits
-- sums. Period is "Current" or "Prior" so QS can render side-by-side bars.

CREATE OR REPLACE VIEW ${database}.period_comparison AS
WITH window_bound AS (
    SELECT MAX(activity_date) AS max_date FROM ${database}.base_user_activity
),
labelled AS (
    SELECT
        b.subscription_tier,
        b.total_messages,
        b.credits_used,
        CASE
            WHEN b.activity_date >  date_add('day', -30, w.max_date) THEN 'Current'
            WHEN b.activity_date >  date_add('day', -60, w.max_date) THEN 'Prior'
            ELSE NULL
        END                                                   AS period,
        CASE
            WHEN b.activity_date >  date_add('day', -30, w.max_date) THEN 2
            WHEN b.activity_date >  date_add('day', -60, w.max_date) THEN 1
            ELSE NULL
        END                                                   AS sort_key
    FROM ${database}.base_user_activity b, window_bound w
    WHERE b.activity_date > date_add('day', -60, w.max_date)
)
SELECT
    period,
    sort_key,
    subscription_tier,
    SUM(total_messages)                  AS messages,
    SUM(credits_used)                    AS credits_used
FROM labelled
WHERE period IS NOT NULL
GROUP BY period, sort_key, subscription_tier;
