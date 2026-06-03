-- User dimension: single source of (user_id, email, user_label, subscription_tier).
-- Other views join this instead of user_totals so the email/label logic is
-- defined once.
--
-- Email handling:
--   • If the source export has no email column, ${email_expr} is NULL and
--     user_label falls back to user_id.
--   • If HashEmails is on, ${email_expr} is sha256(email) - PII does not
--     land in SPICE. Set via the HashEmails CFN parameter; deploy.sh wires the
--     hash expression at view-render time.
--
-- Identity mapping:
--   ${dim_user_label} / ${dim_identity_join} are rendered by build_views.py.
--   OFF: label = email-or-uuid, join empty. ON: label = display_name -> email
--   -> username -> uuid, join = LEFT JOIN identity_map. Same expression as
--   base_user_activity / model_usage so all label sites agree.

CREATE OR REPLACE VIEW ${database}.user_dim AS
WITH email_per_user AS (
    SELECT
        userid                                 AS user_id,
        -- One representative email per user - the most-recent non-empty one.
        MAX(${email_expr})                     AS email,
        MAX(subscription_tier)                 AS subscription_tier_raw
    FROM ${database}.raw_user_report
    GROUP BY userid
)
SELECT
    user_id,
    NULLIF(email, '')                          AS email,
    ${dim_user_label}                          AS user_label,
    CASE upper(COALESCE(subscription_tier_raw, ''))
        WHEN 'PRO'      THEN 'Pro'
        WHEN 'PRO_PLUS' THEN 'Pro+'
        WHEN 'POWER'    THEN 'Power'
        WHEN ''         THEN 'Unknown'
        ELSE subscription_tier_raw
    END                                        AS subscription_tier
FROM email_per_user
${dim_identity_join};
