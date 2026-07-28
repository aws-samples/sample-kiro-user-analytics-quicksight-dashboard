-- date × subscription_tier -> active users.
-- Source for "Daily active users by tier" on the Activity & Trends sheet.
-- Lets us spot tier skew over time (e.g. Pro+ adoption ramping while Pro is
-- flat). Named for the heat map it originally backed; it now feeds a stacked
-- bar, which reads better across a handful of tiers.

CREATE OR REPLACE VIEW ${database}.activity_heatmap AS
SELECT
    activity_date,
    subscription_tier,
    COUNT(DISTINCT user_id)  AS active_users,
    SUM(total_messages)      AS messages,
    SUM(credits_used)        AS credits_used
FROM ${database}.base_user_activity
GROUP BY 1, 2;
