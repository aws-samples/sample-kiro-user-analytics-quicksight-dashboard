-- model_usage: per-model message counts in LONG form.
--
-- Source: report_models, the normalized long-form table that
-- normalize_report_lambda builds by reading the raw Kiro CSVs BY HEADER (one
-- row per date,user,client,model_name,messages). This replaces the old
-- approach of unpivoting dynamic "<model>_messages" columns out of the raw
-- export, which relied on a POSITIONAL Glue table whose column order drifted
-- as Kiro added models and misattributed usage (e.g. "auto" shown as another
-- model). Reading the header-keyed normalized table fixes that at the source.
--
-- user_label comes from a LEFT JOIN onto user_dim (the single source of the
-- per-user label), so model_usage always agrees with base_user_activity /
-- user_dim on a user's label - which the DrillUser parameter depends on - and
-- this view needs no email/identity placeholders of its own.
--
-- Output (unchanged for downstream views): one row per
-- (activity_date, user_id, user_label, client_type, subscription_tier,
--  model_name, messages), messages > 0 only.
CREATE OR REPLACE VIEW ${database}.model_usage AS
SELECT
    CAST(m.date AS date)                            AS activity_date,
    m.userid                                        AS user_id,
    COALESCE(d.user_label, m.userid)                AS user_label,
    upper(m.client_type)                            AS client_type,
    CASE upper(COALESCE(m.subscription_tier, ''))
        WHEN 'PRO'      THEN 'Pro'
        WHEN 'PRO_PLUS' THEN 'Pro+'
        WHEN 'POWER'    THEN 'Power'
        WHEN ''         THEN 'Unknown'
        ELSE m.subscription_tier
    END                                             AS subscription_tier,
    m.model_name                                    AS model_name,
    COALESCE(TRY(CAST(m.messages AS bigint)), 0)    AS messages
FROM ${database}.report_models m
LEFT JOIN ${database}.user_dim d ON d.user_id = m.userid
WHERE COALESCE(TRY(CAST(m.messages AS bigint)), 0) > 0;
