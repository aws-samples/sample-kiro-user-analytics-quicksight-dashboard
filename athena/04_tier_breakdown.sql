CREATE OR REPLACE VIEW ${database}.tier_breakdown AS
SELECT
    activity_date,
    subscription_tier,
    client_type,
    COUNT(DISTINCT user_id)         AS users,
    SUM(total_messages)             AS messages,
    SUM(credits_used)               AS credits_used,
    SUM(overage_credits_used)       AS overage_credits_used,
    SUM(GREATEST(credits_used - overage_credits_used, 0)) AS base_credits_used
FROM ${database}.base_user_activity
GROUP BY 1, 2, 3;
