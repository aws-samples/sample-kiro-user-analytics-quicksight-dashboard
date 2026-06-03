-- Aggregate per user across the full reporting window. The dashboard adds the
-- date filter via QuickSight parameters, so this view does not pre-window.

CREATE OR REPLACE VIEW ${database}.user_totals AS
SELECT
    b.user_id,
    d.user_label,
    d.email,
    d.subscription_tier,
    MAX(b.profile_id)                        AS profile_id,
    MIN(b.activity_date)                     AS first_active_date,
    MAX(b.activity_date)                     AS last_active_date,
    COUNT(DISTINCT b.activity_date)          AS active_days,
    SUM(b.total_messages)                    AS total_messages,
    SUM(b.chat_conversations)                AS total_conversations,
    SUM(b.credits_used)                      AS credits_used,
    SUM(b.overage_credits_used)              AS overage_credits_used,
    BOOL_OR(b.overage_enabled)               AS overage_enabled
FROM ${database}.base_user_activity b
LEFT JOIN ${database}.user_dim d ON d.user_id = b.user_id
GROUP BY b.user_id, d.user_label, d.email, d.subscription_tier;
