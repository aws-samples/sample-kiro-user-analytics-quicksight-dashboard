-- Curated base view over the normalized Kiro user report.
--
-- Source: report_facts, the fixed-schema table that normalize_report_lambda
-- builds by reading the raw Kiro CSVs BY HEADER. Reading the raw export
-- directly used a POSITIONAL Glue table whose middle columns drift as Kiro
-- adds models / toggles the trailing New_User column, which corrupted
-- new_user and the per-model split. report_facts carries only the stable
-- scalar columns in a fixed order, so this view is immune to that drift.
--
-- Deduplication is already done in the Lambda (latest export wins per
-- date,user,client), so there is no $$path dedup here.
--
-- `email` is always present in report_facts (blank when the source export
-- predates the April-2026 email column), so ${email_expr} resolves to the
-- column ref (or its sha256 hash when HashEmails is on).
--
-- ${base_user_label} / ${base_identity_join} are rendered by build_views.py:
--   • identity mapping OFF: label = email-or-uuid, join = empty.
--   • identity mapping ON:  label = display_name -> email -> username -> uuid,
--     join = LEFT JOIN identity_map im ON im.idc_user_id = userid.
-- The exact same expression is rendered into user_dim so the label sites agree
-- (the DrillUser parameter depends on that).
CREATE OR REPLACE VIEW ${database}.base_user_activity AS
SELECT
    CAST(date AS date)                                AS activity_date,
    userid                                            AS user_id,
    COALESCE(${email_expr}, '')                       AS email,
    ${base_user_label}                                AS user_label,
    upper(client_type)                                AS client_type,
    CASE upper(COALESCE(subscription_tier, ''))
        WHEN 'PRO'      THEN 'Pro'
        WHEN 'PRO_PLUS' THEN 'Pro+'
        WHEN 'POWER'    THEN 'Power'
        WHEN ''         THEN 'Unknown'
        ELSE subscription_tier
    END                                               AS subscription_tier,
    -- Per-user constant tier: a single tier per user across the whole window,
    -- so the All-users table can group by user_label and show one row even
    -- when a user changed tier mid-period (e.g. Pro+ -> Power). We use the
    -- tier from the user's MOST RECENT activity day (max_by tier over date) =
    -- their CURRENT plan, so an upgrade shows immediately. (A lexical MAX was
    -- wrong: 'PRO_PLUS' > 'POWER' alphabetically, so an upgrade to Power kept
    -- showing Pro+.) Same most-recent rule as user_dim.
    CASE upper(max_by(COALESCE(subscription_tier, ''), date) OVER (PARTITION BY userid))
        WHEN 'PRO'      THEN 'Pro'
        WHEN 'PRO_PLUS' THEN 'Pro+'
        WHEN 'POWER'    THEN 'Power'
        WHEN ''         THEN 'Unknown'
        ELSE subscription_tier
    END                                               AS user_tier,
    profileid                                         AS profile_id,
    COALESCE(TRY(CAST(new_user AS boolean)), false)   AS new_user,
    COALESCE(TRY(CAST(chat_conversations AS bigint)), 0)    AS chat_conversations,
    COALESCE(TRY(CAST(total_messages     AS bigint)), 0)    AS total_messages,
    COALESCE(TRY(CAST(credits_used       AS double)), 0.0)  AS credits_used,
    COALESCE(TRY(CAST(overage_cap        AS double)), 0.0)  AS overage_cap,
    COALESCE(TRY(CAST(overage_credits_used AS double)), 0.0) AS overage_credits_used,
    COALESCE(TRY(CAST(overage_enabled    AS boolean)), false) AS overage_enabled
FROM ${database}.report_facts
${base_identity_join};
