-- date × subscription_tier -> active users.
-- Source for the activity heatmap on the Executive sheet. Lets us spot tier
-- skew over time (e.g. PRO_PLUS adoption ramping while PRO is flat).

CREATE OR REPLACE VIEW ${database}.activity_heatmap AS
SELECT
    activity_date,
    subscription_tier,
    COUNT(DISTINCT user_id)  AS active_users,
    SUM(total_messages)      AS messages,
    SUM(credits_used)        AS credits_used
FROM ${database}.base_user_activity
GROUP BY 1, 2;
