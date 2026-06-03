CREATE OR REPLACE VIEW ${database}.daily_trends AS
SELECT
    activity_date,
    client_type,
    COUNT(DISTINCT user_id)               AS active_users,
    SUM(total_messages)                   AS total_messages,
    SUM(chat_conversations)               AS total_conversations,
    SUM(credits_used)                     AS credits_used,
    SUM(overage_credits_used)             AS overage_credits_used,
    -- new vs returning split: base_user_activity is one row per
    -- (user, date, client_type), so within each (date, client_type) group a
    -- user appears once. new_users counts rows flagged new_user; returning is
    -- the remainder of the distinct active users. Powers the Activity-sheet
    -- "Daily new vs returning users" stacked area.
    SUM(CASE WHEN new_user THEN 1 ELSE 0 END) AS new_users,
    COUNT(DISTINCT user_id) - SUM(CASE WHEN new_user THEN 1 ELSE 0 END) AS returning_users
FROM ${database}.base_user_activity
GROUP BY 1, 2;
