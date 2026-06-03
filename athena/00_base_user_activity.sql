-- Curated base view over the Kiro user_report.
-- Verified against sample: KIRO_IDE_<acct>_user_report_<ts>.csv (May 2026).
-- Sample columns (in order):
--   Date, UserId, Client_Type, Chat_Conversations, Credits_Used, Overage_Cap,
--   Overage_Credits_Used, Overage_Enabled, ProfileId, Subscription_Tier,
--   Total_Messages, <model>_messages..., New_User
--
-- `email` is documented for post–April 2026 exports but is NOT present in
-- this sample, so we read it via TRY/COALESCE and tolerate its absence.
--
-- Deduplication: customers with re-runs, multi-region exports, or backfill
-- overlap will see multiple rows for the same (user_id, date, client_type).
-- We keep the row from the lexically-latest file path (Athena's path
-- pseudo-column = the S3 URI), which matches "most recent export" given the
-- timestamp suffix in the filename.

-- Note: user_label is exposed so the User-detail sheet can filter `base`
-- against the DrillUser parameter (which holds a label, not a UUID). We
-- can't join to user_dim here because user_dim is downstream - instead
-- the label is computed inline (same logic as user_dim).
--
-- ${base_user_label} / ${base_identity_join} are rendered by build_views.py:
--   • identity mapping OFF: label = email-or-uuid, join = empty.
--   • identity mapping ON:  label = display_name -> email -> username -> uuid,
--     join = LEFT JOIN identity_map im ON im.idc_user_id = userid.
-- The exact same expression is rendered into user_dim and model_usage so all
-- three label sites agree (the DrillUser parameter depends on that).
CREATE OR REPLACE VIEW ${database}.base_user_activity AS
WITH deduped AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY userid, date, client_type
               ORDER BY "$$path" DESC
           ) AS dedupe_rn
    FROM ${database}.raw_user_report
)
SELECT
    CAST(date AS date)                                AS activity_date,
    userid                                            AS user_id,
    -- email column added April 2026; absent in older exports - tolerate.
    COALESCE(${email_expr}, '')                       AS email,
    -- user_label: see header note; identical expression to user_dim so the
    -- DrillUser parameter resolves across views.
    ${base_user_label}                                AS user_label,
    upper(client_type)                                AS client_type,
    -- Sample shows tier as upper snake (PRO_PLUS); normalize for display.
    CASE upper(COALESCE(subscription_tier, ''))
        WHEN 'PRO'      THEN 'Pro'
        WHEN 'PRO_PLUS' THEN 'Pro+'
        WHEN 'POWER'    THEN 'Power'
        WHEN ''         THEN 'Unknown'
        ELSE subscription_tier
    END                                               AS subscription_tier,
    profileid                                         AS profile_id,
    COALESCE(TRY(CAST(new_user AS boolean)), false)   AS new_user,
    COALESCE(TRY(CAST(chat_conversations AS bigint)), 0)    AS chat_conversations,
    COALESCE(TRY(CAST(total_messages     AS bigint)), 0)    AS total_messages,
    COALESCE(TRY(CAST(credits_used       AS double)), 0.0)  AS credits_used,
    COALESCE(TRY(CAST(overage_cap        AS double)), 0.0)  AS overage_cap,
    COALESCE(TRY(CAST(overage_credits_used AS double)), 0.0) AS overage_credits_used,
    COALESCE(TRY(CAST(overage_enabled    AS boolean)), false) AS overage_enabled
FROM deduped
${base_identity_join}
WHERE dedupe_rn = 1;
