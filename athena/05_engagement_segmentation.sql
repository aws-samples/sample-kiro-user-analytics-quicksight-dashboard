-- Mirror the segmentation buckets the aws-samples Streamlit app uses.
-- Power  >= 20 active days OR >= 1000 messages in the trailing 30d window
-- Active >= 8 active days
-- Light  >= 1 active day
-- Idle   no activity in trailing 30d but seen earlier
--
-- The 30-day window is computed from MAX(activity_date) so it tracks the
-- latest export rather than wall-clock time.
--
-- subscription_tier is sourced from user_dim, which uses MAX(subscription_tier)
-- across the user's history (most-recent dominates). This makes the segment
-- column tier-aware so the People-sheet tier picker can filter the donut.

CREATE OR REPLACE VIEW ${database}.engagement_segmentation AS
WITH window_bound AS (
    SELECT MAX(activity_date) AS max_date FROM ${database}.base_user_activity
),
recent AS (
    SELECT
        b.user_id,
        COUNT(DISTINCT b.activity_date) AS active_days_30d,
        SUM(b.total_messages)           AS messages_30d
    FROM ${database}.base_user_activity b, window_bound w
    WHERE b.activity_date >= date_add('day', -30, w.max_date)
    GROUP BY b.user_id
),
ever_seen AS (
    SELECT user_id FROM ${database}.base_user_activity GROUP BY user_id
)
SELECT
    e.user_id,
    d.user_label,
    d.subscription_tier,
    COALESCE(r.active_days_30d, 0)            AS active_days_30d,
    COALESCE(r.messages_30d, 0)               AS messages_30d,
    -- 1 if the user had any activity in the trailing 30d, else 0. Lets the
    -- Executive seat-utilization KPI compute active/total as AVERAGE(is_active).
    CASE WHEN COALESCE(r.active_days_30d, 0) >= 1 THEN 1 ELSE 0 END AS is_active,
    CASE
        WHEN COALESCE(r.active_days_30d, 0) >= 20
          OR COALESCE(r.messages_30d, 0)    >= 1000 THEN 'Power'
        WHEN COALESCE(r.active_days_30d, 0) >= 8    THEN 'Active'
        WHEN COALESCE(r.active_days_30d, 0) >= 1    THEN 'Light'
        ELSE 'Idle'
    END                                       AS segment
FROM ever_seen e
LEFT JOIN recent r ON r.user_id = e.user_id
LEFT JOIN ${database}.user_dim d ON d.user_id = e.user_id;
