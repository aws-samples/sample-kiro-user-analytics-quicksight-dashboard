-- Week-over-week movers: per-user change in messages, last 7d vs prior 7d.
-- No filter - both climbers and droppers are interesting. Dashboard sorts
-- absolute message_delta to surface the biggest movement either way.

CREATE OR REPLACE VIEW ${database}.wow_movers AS
WITH window_bound AS (
    SELECT MAX(activity_date) AS max_date FROM ${database}.base_user_activity
),
recent AS (
    SELECT b.user_id, SUM(b.total_messages) AS messages
    FROM ${database}.base_user_activity b, window_bound w
    WHERE b.activity_date >= date_add('day', -7, w.max_date)
    GROUP BY b.user_id
),
prior AS (
    SELECT b.user_id, SUM(b.total_messages) AS messages
    FROM ${database}.base_user_activity b, window_bound w
    WHERE b.activity_date >= date_add('day', -14, w.max_date)
      AND b.activity_date <  date_add('day',  -7, w.max_date)
    GROUP BY b.user_id
),
combined AS (
    SELECT
        COALESCE(p.user_id, r.user_id)        AS user_id,
        COALESCE(p.messages, 0)               AS prior_messages,
        COALESCE(r.messages, 0)               AS recent_messages
    FROM prior p
    FULL OUTER JOIN recent r ON r.user_id = p.user_id
)
SELECT
    c.user_id,
    d.user_label,
    d.subscription_tier,
    c.prior_messages,
    c.recent_messages,
    (c.recent_messages - c.prior_messages)                          AS message_delta,
    abs(c.recent_messages - c.prior_messages)                       AS abs_delta,
    CASE WHEN c.prior_messages > 0
         THEN CAST(c.recent_messages - c.prior_messages AS double)
              / c.prior_messages
         ELSE NULL
    END                                                             AS pct_change,
    -- At-risk flag: was active last week, dropped > 50% this week.
    CASE WHEN c.prior_messages > 0
              AND c.recent_messages < c.prior_messages * 0.5
         THEN 'Yes' ELSE 'No'
    END                                                             AS at_risk
FROM combined c
LEFT JOIN ${database}.user_dim d ON d.user_id = c.user_id;
