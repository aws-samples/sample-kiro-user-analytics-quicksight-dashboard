-- Per-model message counts live in dynamic columns named "<model>_messages".
-- Athena cannot reference column names from a sibling row, so we cannot do
-- the unpivot in pure SQL. Instead, scripts/build_views.py inspects the
-- live AWS Glue table, discovers the model columns, and renders one
-- "SELECT ... UNION ALL" branch per column into a placeholder.
--
-- Output: one row per (date, user_id, client_type, model_name, messages),
-- including only rows where messages > 0.

CREATE OR REPLACE VIEW ${database}.model_usage AS
${model_union};
