-- Dense per-user daily series for the User-detail daily charts.
--
-- The raw activity only has rows for days a user was active, so a bar chart
-- over it SKIPS no-activity days - making sparse usage (weekends off, gaps)
-- look continuous ("used it 5 days straight" when really 2 days + a gap).
-- This view fills the gaps: for each user it generates one row PER CALENDAR
-- DAY between their first and last active day, zero-filling the days with no
-- activity. The User-detail daily Messages/Credits bars read this so empty
-- days render as visible zero (or absent-height) bars in the real position.
--
-- Bounded by each user's own [first_active, last_active] span (not a global
-- calendar x all users), so the row count stays proportional to real activity
-- spans, not users x total-days. user_label is carried so the DrillUser filter
-- and the date-range picker apply exactly as they do on the other base-backed
-- User-detail visuals.
CREATE OR REPLACE VIEW ${database}.user_daily_dense AS
-- Group by user_id ALONE, never by (user_id, user_label). base_user_activity
-- makes user_label per-user constant, but grouping on it here would make this
-- view's correctness depend on that invariant holding upstream: if a user ever
-- carried two labels they would get two calendar spans, while the `daily` CTE
-- below groups by user_id only, so the LEFT JOIN would match each day's totals
-- to BOTH spans and double every value on the User-detail bars. Taking the
-- label from user_dim at the end removes that coupling entirely - this view
-- cannot fan out regardless of what base does.
WITH per_user AS (
    SELECT
        user_id,
        MIN(activity_date) AS first_day,
        MAX(activity_date) AS last_day
    FROM ${database}.base_user_activity
    GROUP BY user_id
),
-- One row per (user, calendar day) across the user's active span.
calendar AS (
    SELECT
        p.user_id,
        -- sequence(date, date, INTERVAL '1' DAY) yields TIMESTAMP elements,
        -- so cast back to DATE so activity_date stays a date (a timestamp(0)
        -- column errors downstream in Athena/QuickSight).
        CAST(d AS date) AS activity_date
    FROM per_user p
    CROSS JOIN UNNEST(
        sequence(p.first_day, p.last_day, INTERVAL '1' DAY)
    ) AS t(d)
),
-- Collapse the per-(user,day,client) base rows to one per (user, day).
daily AS (
    SELECT
        user_id,
        activity_date,
        SUM(total_messages) AS total_messages,
        SUM(credits_used)   AS credits_used
    FROM ${database}.base_user_activity
    GROUP BY user_id, activity_date
)
SELECT
    c.user_id,
    -- The label comes from user_dim, the single source of the per-user label
    -- (one row per user_id by construction, so this join cannot fan out).
    -- COALESCE to the id so a user missing from user_dim still renders.
    COALESCE(u.user_label, c.user_id) AS user_label,
    c.activity_date,
    COALESCE(d.total_messages, 0)  AS total_messages,
    COALESCE(d.credits_used, 0.0)  AS credits_used
FROM calendar c
LEFT JOIN daily d
  ON d.user_id = c.user_id
 AND d.activity_date = c.activity_date
LEFT JOIN ${database}.user_dim u
  ON u.user_id = c.user_id;
