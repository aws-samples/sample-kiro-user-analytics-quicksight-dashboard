#!/usr/bin/env python3
"""
Render and execute the Athena views in athena/.

First creates the report_facts / report_models external tables over the
normalized CSVs that normalize_report_lambda lands in the results bucket, then
renders the views (which read those tables). Views only need ${database} and a
few label placeholders substituted; the old per-model UNION-ALL melt is gone -
model_usage now reads the long-form report_models table directly.

Usage:
    python scripts/build_views.py \\
        --database kiro_analytics \\
        --workgroup kiro_analytics \\
        --region us-east-1 \\
        --normalized-bucket <athena-results-bucket>

Requires AWS credentials with athena:StartQueryExecution on the workgroup.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import string
import sys
import time

import boto3

VIEWS_DIR = pathlib.Path(__file__).resolve().parent.parent / "athena"


def email_expression(hash_emails: bool) -> str:
    """report_facts always carries an `email` column (blank when the source
    export predates the April-2026 email field), so we always reference it.

    If hash_emails is True, wrap it in to_hex(sha256(...)) so PII does not
    reach SPICE - the view layer then sees opaque hex digests. NULLIF maps the
    empty-string blank to NULL first so a hashed blank doesn't become a
    constant non-empty digest.
    """
    if hash_emails:
        return "to_hex(sha256(CAST(NULLIF(email, '') AS varbinary)))"
    return "email"


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# S3 bucket naming rules (lowercase, digits, hyphens, dots; 3-63 chars).
_S3_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
# A simple S3 key prefix: path segments of safe chars, no leading/trailing /.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9!_.\-]+(?:/[A-Za-z0-9!_.\-]+)*$")


def _validate_identifier(value: str, kind: str) -> str:
    """Reject anything that isn't a plain SQL identifier. Defense in depth
    for the SQL we render below: callers already pass values from trusted
    sources (CFN parameters validated by AllowedPattern; AWS Glue
    get-table response), but we re-check here so an unexpected name from a
    future AWS Glue release can't slip a quote / semicolon into a CREATE VIEW."""
    if not _IDENT_RE.match(value):
        raise ValueError(f"Invalid {kind} identifier: {value!r}")
    return value


# --- Normalized report tables -----------------------------------------------
# normalize_report_lambda parses the raw Kiro CSVs by HEADER and lands one
# output part per source file, partitioned by export_date, under:
#   normalized/facts/export_date=YYYY-MM-DD/part-<hash>.csv
#   normalized/models/export_date=YYYY-MM-DD/part-<hash>.csv
# Every row carries src_path + export_ts bookkeeping columns.
#
# We register each as a PARTITIONED external table (report_facts_raw /
# report_models_raw) with partition PROJECTION on export_date - so no
# MSCK REPAIR / ADD PARTITION is ever needed - then layer a dedup VIEW
# (report_facts / report_models) on top that keeps only the latest export per
# key via ROW_NUMBER(). The rest of the pipeline reads the deduped views and is
# unchanged. OpenCSVSerDe is positional, but safe here: WE author the parts
# with a fixed, known column order (unlike the raw export, whose middle columns
# drift). Dedup lives in SQL (where it scales to any history size) while the
# header-binding lives in the Lambda (the one thing Athena CSV cannot do).

def report_tables_ddl(database: str, bucket: str, prefix: str) -> list[str]:
    """DROP + CREATE for the report_facts/report_models dedup views and their
    underlying partitioned raw external tables. Returns statements in apply
    order (drop views before tables; create tables before views)."""
    _validate_identifier(database, "database")
    if not _S3_NAME_RE.match(bucket):
        raise ValueError(f"Invalid normalized-data bucket name: {bucket!r}")
    clean_prefix = prefix.strip("/")
    if not _PREFIX_RE.match(clean_prefix):
        raise ValueError(f"Invalid normalized-data prefix: {prefix!r}")
    facts_loc = f"s3://{bucket}/{clean_prefix}/facts/"
    models_loc = f"s3://{bucket}/{clean_prefix}/models/"
    # Partition projection bounds: export_date is a DATE partition projected
    # over a wide static range so any real export date resolves without
    # registering partitions. The lower bound predates Kiro; the upper bound is
    # far future. NOW is allowed so future-dated test data still projects.
    proj = (
        "    'projection.enabled' = 'true',\n"
        "    'projection.export_date.type' = 'date',\n"
        "    'projection.export_date.format' = 'yyyy-MM-dd',\n"
        "    'projection.export_date.range' = '2020-01-01,NOW+1YEARS',\n"
        "    'projection.export_date.interval' = '1',\n"
        "    'projection.export_date.interval.unit' = 'DAYS',\n"
        "    'storage.location.template' = '{loc}export_date=${{export_date}}/',\n"
        "    'skip.header.line.count' = '1'"
    )
    # nosec B608 - database validated; bucket/prefix regex-validated above;
    # every other token is a literal. Athena DDL has no parameter binding.
    return [
        # Drop report_facts/report_models in BOTH forms: a prior deploy created
        # them as TABLES (the old non-partitioned design); this design makes
        # them VIEWS. DROP VIEW won't remove a table and vice-versa, so issue
        # both (IF EXISTS makes the non-matching one a no-op). Drop the
        # dependent views/tables before the underlying raw tables.
        f"DROP VIEW IF EXISTS {database}.report_facts",  # nosec B608
        f"DROP VIEW IF EXISTS {database}.report_models",  # nosec B608
        f"DROP TABLE IF EXISTS {database}.report_facts",  # nosec B608
        f"DROP TABLE IF EXISTS {database}.report_models",  # nosec B608
        f"DROP TABLE IF EXISTS {database}.report_facts_raw",  # nosec B608
        f"DROP TABLE IF EXISTS {database}.report_models_raw",  # nosec B608
        (
            f"CREATE EXTERNAL TABLE {database}.report_facts_raw (\n"  # nosec B608
            f"    date                 string,\n"
            f"    userid               string,\n"
            f"    client_type          string,\n"
            f"    chat_conversations   string,\n"
            f"    credits_used         string,\n"
            f"    overage_cap          string,\n"
            f"    overage_credits_used string,\n"
            f"    overage_enabled      string,\n"
            f"    profileid            string,\n"
            f"    subscription_tier    string,\n"
            f"    total_messages       string,\n"
            f"    new_user             string,\n"
            f"    email                string,\n"
            f"    src_path             string,\n"
            f"    export_ts            string\n"
            f")\n"
            f"PARTITIONED BY (export_date string)\n"
            f"ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'\n"
            f"WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '\"')\n"
            f"LOCATION '{facts_loc}'\n"
            f"TBLPROPERTIES (\n{proj.format(loc=facts_loc)}\n)"
        ),
        (
            f"CREATE EXTERNAL TABLE {database}.report_models_raw (\n"  # nosec B608
            f"    date              string,\n"
            f"    userid            string,\n"
            f"    client_type       string,\n"
            f"    subscription_tier string,\n"
            f"    model_name        string,\n"
            f"    messages          string,\n"
            f"    src_path          string,\n"
            f"    export_ts         string\n"
            f")\n"
            f"PARTITIONED BY (export_date string)\n"
            f"ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'\n"
            f"WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '\"')\n"
            f"LOCATION '{models_loc}'\n"
            f"TBLPROPERTIES (\n{proj.format(loc=models_loc)}\n)"
        ),
        # Dedup view: one fact row per (date,user,client) - the latest export
        # wins (highest export_ts, then src_path as a tiebreaker). This is the
        # latest-export-wins rule the old base_user_activity applied via the
        # "$path" pseudo-column, now done over the partitioned parts.
        (
            f"CREATE OR REPLACE VIEW {database}.report_facts AS\n"  # nosec B608
            f"SELECT date, userid, client_type, chat_conversations, credits_used,\n"
            f"       overage_cap, overage_credits_used, overage_enabled, profileid,\n"
            f"       subscription_tier, total_messages, new_user, email\n"
            f"FROM (\n"
            f"  SELECT *, ROW_NUMBER() OVER (\n"
            f"      PARTITION BY date, userid, upper(client_type)\n"
            f"      ORDER BY export_ts DESC, src_path DESC\n"
            f"  ) AS rn\n"
            f"  FROM {database}.report_facts_raw\n"
            f")\n"
            f"WHERE rn = 1"
        ),
        # Dedup view for models: the authoritative snapshot for a (date,user,
        # client) is the latest export's FILE, so we rank by the same key (NOT
        # including model_name) and keep all model rows from the winning file.
        # Ranking by the file's (export_ts, src_path) and keeping rn over the
        # per-file row set would need the file id; instead we pick the winning
        # (export_ts, src_path) per key from the facts ranking and join.
        (
            f"CREATE OR REPLACE VIEW {database}.report_models AS\n"  # nosec B608
            f"WITH winning AS (\n"
            f"  SELECT date, userid, upper(client_type) AS client_u,\n"
            f"         max_by(src_path, (export_ts, src_path)) AS win_src\n"
            f"  FROM {database}.report_facts_raw\n"
            f"  GROUP BY date, userid, upper(client_type)\n"
            f")\n"
            f"SELECT m.date, m.userid, m.client_type, m.subscription_tier,\n"
            f"       m.model_name, m.messages\n"
            f"FROM {database}.report_models_raw m\n"
            f"JOIN winning w\n"
            f"  ON m.date = w.date AND m.userid = w.userid\n"
            f" AND upper(m.client_type) = w.client_u\n"
            f" AND m.src_path = w.win_src"
        ),
    ]


# --- Identity mapping (optional) --------------------------------------------
# When deploy.sh enables IAM Identity Center user mapping it passes
# --identity-map-bucket. We then (a) create an `identity_map` external table
# over the CSV the identity-map Lambda lands in S3, and (b) rewrite the
# user_label expression in base_user_activity / user_dim / model_usage to
# prefer the resolved human identity, falling back to email then the UUID.
#
# The external table's columns are deliberately `idc_`-prefixed so a LEFT JOIN
# onto any existing view never collides with that view's own `user_id` /
# `email` columns - OpenCSVSerDe maps columns by position, not by the CSV
# header names, so the prefix is free.

def identity_map_ddl(database: str, bucket: str, prefix: str) -> list[str]:
    """DROP + CREATE for the identity_map external table. Returns the two
    statements to run (drop first for idempotent re-deploys; dropping an
    EXTERNAL table leaves the S3 data untouched)."""
    _validate_identifier(database, "database")
    if not _S3_NAME_RE.match(bucket):
        raise ValueError(f"Invalid identity-map bucket name: {bucket!r}")
    # Normalise to a single trailing slash; LOCATION must be the prefix
    # "directory", e.g. s3://my-bucket/identity-map/
    clean_prefix = prefix.strip("/")
    if not _PREFIX_RE.match(clean_prefix):
        raise ValueError(f"Invalid identity-map prefix: {prefix!r}")
    location = f"s3://{bucket}/{clean_prefix}/"
    drop = f"DROP TABLE IF EXISTS {database}.identity_map"  # nosec B608
    # nosec B608 - database validated by _validate_identifier; bucket/prefix
    # validated by the regexes above; all other tokens are literals. Athena
    # DDL has no parameter binding.
    create = (
        f"CREATE EXTERNAL TABLE {database}.identity_map (\n"  # nosec B608
        f"    idc_user_id      string,\n"
        f"    idc_username     string,\n"
        f"    idc_display_name string,\n"
        f"    idc_email        string\n"
        f")\n"
        f"ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'\n"
        f"WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '\"')\n"
        f"LOCATION '{location}'\n"
        f"TBLPROPERTIES ('skip.header.line.count' = '1')"
    )
    return [drop, create]


# Raw subscription_tier value -> display label. The Kiro export emits
# SCREAMING_SNAKE tier codes; the dashboard shows friendly labels. Kiro has
# exactly four paid tiers - Pro, Pro+, Pro Max, Power - so this is the complete
# set for an enterprise subscription. (Free is deliberately absent: it exists
# only on Builder IDs, and an enterprise can only buy Pro and above, so it
# cannot appear in this export.) 'Unknown' below covers a blank/missing tier,
# not a real plan. Keep in sync
# with _TIER_COLORS in create_dashboard.py, which pins a fixed colour per label
# (an unlisted label still renders, just from the auto palette).
_TIER_LABELS = {
    "PRO": "Pro",
    "PRO_PLUS": "Pro+",
    "PRO_MAX": "Pro Max",
    "POWER": "Power",
}


def tier_label_expr(raw: str) -> str:
    """A CASE expression mapping a raw tier value to its display label.

    `raw` is the SQL expression holding the raw tier - a plain column
    (`subscription_tier`) or a window function (`max_by(...) OVER (...)`). It is
    substituted at EVERY position, including the fallback, which is the whole
    point: the previous hand-written CASEs ended in `ELSE subscription_tier`,
    a *different* expression than the one being tested. Two bugs came out of
    that, both visible as a column mixing 'Pro+' with 'PRO_PLUS':

      1. An unmapped tier (e.g. PRO_MAX, which Kiro added after this was
         written) fell through to the raw SCREAMING_SNAKE value, so one column
         showed both styles at once.
      2. Worse, in the `user_tier` case the CASE tested the per-user windowed
         tier but the ELSE returned the PER-ROW tier. For a user on an unmapped
         tier, user_tier stopped being constant per user - which silently broke
         the one-row-per-user guarantee the All-users table relies on (a user
         who changed tier mid-window split into several rows, each with a
         partial share of their totals).

    Rather than only adding the missing WHENs, unknown tiers now get a
    generic prettifier (PRO_ULTRA -> 'Pro Ultra'), so the next tier Kiro
    invents renders sensibly on a dashboard nobody has redeployed yet. Empty
    and NULL both read 'Unknown'.
    """
    operand = f"upper(COALESCE({raw}, ''))"
    whens = "\n".join(
        f"        WHEN '{code}' THEN '{label}'"
        for code, label in _TIER_LABELS.items()
    )
    # Title-case each '_'-separated word of the raw value: split on the
    # separator, upper the first letter, lower the rest, re-join with spaces.
    fallback = (
        f"array_join(transform(split(replace({operand}, '_', ' '), ' '),"
        f" w -> upper(substr(w, 1, 1)) || lower(substr(w, 2))), ' ')"
    )
    return (
        f"CASE {operand}\n"
        f"        WHEN '' THEN 'Unknown'\n"
        f"{whens}\n"
        f"        ELSE {fallback}\n"
        f"    END"
    )


def _identity_map_source(database: str) -> str:
    """The identity_map side of the join, collapsed to ONE ROW PER idc_user_id.

    The external table's LOCATION is a *prefix*, so any extra object landing
    under it (a partial write, a manual copy, a second export) would repeat an
    idc_user_id. Because idc_username / idc_email are grouping columns in
    user_totals, a duplicated id would fan that user out into multiple rows -
    each carrying a full copy of their SUM totals. Collapsing here keeps the
    join 1:1 by construction rather than by luck.
    """
    return (
        f"(SELECT idc_user_id,"
        f" MAX(idc_username) AS idc_username,"
        f" MAX(idc_display_name) AS idc_display_name,"
        f" MAX(idc_email) AS idc_email"
        f" FROM {database}.identity_map GROUP BY idc_user_id)"
    )


def render_identity_label_parts(database: str, email_expr: str, enabled: bool) -> dict[str, str]:
    """Build the template substitutions that switch user_label between the
    plain (email-or-uuid) and identity-resolved forms, plus the LEFT JOIN
    clauses each view needs. The same precedence
    (display_name -> email -> username -> uuid) is rendered into all three
    label sites so they stay consistent.

    Returned keys:
      base_user_label / base_identity_join  - for base_user_activity
      dim_user_label  / dim_identity_join   - for user_dim (email_per_user CTE)
      dim_idc_username / dim_idc_email      - for user_dim, the raw Identity
          Center join keys (see below)
    (model_usage derives its label by LEFT JOIN onto user_dim, so it needs no
    placeholders of its own.)

    Identity JOIN KEYS (dim_idc_username / dim_idc_email): user_label alone is a
    display string (a real name when mapping is on), which cannot be joined
    against an external roster/subscription export. We therefore also surface
    the directory username and email as their own columns so admins can map
    dashboard rows to their own user lists. When mapping is OFF these resolve to
    typed NULLs so user_dim keeps a STABLE column set in both modes - the
    QuickSight DataSet declares these columns unconditionally, and a missing
    column would break SPICE ingestion.
    """
    if not enabled:
        return {
            "base_user_label": f"COALESCE(NULLIF({email_expr}, ''), userid)",
            "base_identity_join": "",
            "dim_user_label": "COALESCE(NULLIF(email, ''), user_id)",
            "dim_identity_join": "",
            "dim_idc_username": "CAST(NULL AS varchar)",
            "dim_idc_email": "CAST(NULL AS varchar)",
            "base_idc_username": "CAST(NULL AS varchar)",
            "base_idc_email": "CAST(NULL AS varchar)",
        }
    _validate_identifier(database, "database")
    # Some source rows have historically stored userid with surrounding double
    # quotes ("<guid>", length 38 not 36), while the Identity Store UserId
    # (idc_user_id) is the bare guid. We therefore strip any wrapping quotes
    # from the report side of the JOIN with trim(both '"' ...) - a no-op when
    # the value isn't quoted, so it's safe either way. The Lambda's
    # distinct-user query applies the identical
    # normalisation so the persisted map keys match. We only normalise on the
    # JOIN comparison; the userid/user_id output columns are left untouched so
    # the rest of the (internally consistent) views and the DrillUser parameter
    # are unaffected.
    return {
        "base_user_label": (
            f"COALESCE(NULLIF(im.idc_display_name, ''), NULLIF({email_expr}, ''), "
            f"NULLIF(im.idc_username, ''), userid)"
        ),
        "base_identity_join": (
            f"LEFT JOIN {_identity_map_source(database)} im "
            f"ON im.idc_user_id = trim(both '\"' from userid)"
        ),
        "dim_user_label": (
            "COALESCE(NULLIF(im.idc_display_name, ''), NULLIF(email, ''), "
            "NULLIF(im.idc_username, ''), user_id)"
        ),
        "dim_identity_join": (
            f"LEFT JOIN {_identity_map_source(database)} im "
            f"ON im.idc_user_id = trim(both '\"' from user_id)"
        ),
        # Raw directory join keys, exposed as their own columns (NULLIF so an
        # empty directory field reads as NULL rather than ''). Both the base
        # and dim variants read the same joined identity_map alias `im`; they
        # are separate keys only because the two views substitute independently.
        "dim_idc_username": "NULLIF(im.idc_username, '')",
        "dim_idc_email": "NULLIF(im.idc_email, '')",
        "base_idc_username": "NULLIF(im.idc_username, '')",
        "base_idc_email": "NULLIF(im.idc_email, '')",
    }


def split_statements(sql: str) -> list[str]:
    """Split a .sql file into individual statements for Athena (which accepts
    only one statement per query).

    We split on a ';' that ENDS A LINE, but only when that ';' is real SQL - a
    ';' inside a `--` comment is ignored. Naively splitting on ";\\n" silently
    truncated a view whose *comment* happened to end a line with ';' (e.g.
    "-- ... (often empty);"), producing a baffling "mismatched input '<EOF>'"
    parse error. Comment text is prose and must never affect statement
    boundaries."""
    statements: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        current.append(line)
        # Strip a trailing line comment before deciding if the line ends a
        # statement. (String literals in these views never contain '--'.)
        code = line.split("--", 1)[0].rstrip()
        if code.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def run_athena(athena, sql: str, workgroup: str, database: str) -> None:
    qid = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        WorkGroup=workgroup,
    )["QueryExecutionId"]
    while True:
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if state["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state["State"] != "SUCCEEDED":
        raise SystemExit(
            f"Athena query {qid} ended in {state['State']}: "
            f"{state.get('StateChangeReason', '')}"
        )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--workgroup", required=True)
    p.add_argument("--region", required=True)
    p.add_argument(
        "--normalized-bucket",
        required=True,
        help="Bucket where normalize_report_lambda lands the normalized CSVs "
             "(the Athena results bucket). report_facts / report_models "
             "external tables are created over it.",
    )
    p.add_argument(
        "--normalized-prefix",
        default="normalized",
        help="S3 key prefix (directory) of the normalized output within "
             "--normalized-bucket. Default: normalized",
    )
    p.add_argument(
        "--tables-only",
        action="store_true",
        help="Create ONLY the report_facts / report_models external tables, "
             "then exit (skip the views). deploy.sh runs this before the "
             "identity-map Lambda so it can read report_facts, then runs the "
             "full build afterward.",
    )
    p.add_argument(
        "--hash-emails",
        action="store_true",
        help="Hash email values with SHA-256 at the view layer. Use when "
             "policy disallows plaintext email in SPICE.",
    )
    p.add_argument(
        "--identity-map-bucket",
        default="",
        help="Enable IAM Identity Center user mapping: the bucket where the "
             "identity-map Lambda lands its CSV. When set, an `identity_map` "
             "external table is created and user_label resolves to the human "
             "identity. Mutually exclusive with --hash-emails.",
    )
    p.add_argument(
        "--identity-map-prefix",
        default="identity-map",
        help="S3 key prefix (directory) of the identity-map CSV within "
             "--identity-map-bucket. Default: identity-map",
    )
    args = p.parse_args()

    identity_enabled = bool(args.identity_map_bucket)
    if identity_enabled and args.hash_emails:
        # Resolving users to real names while hashing their email is
        # contradictory, and writing plaintext display_name next to a hashed
        # email would silently defeat the privacy control. deploy.sh guards
        # this too; this is defence in depth.
        p.error("--hash-emails and --identity-map-bucket are mutually exclusive.")

    athena = boto3.client("athena", region_name=args.region)

    email_expr = email_expression(args.hash_emails)
    print(
        f"email: {'hashed' if args.hash_emails else 'plaintext'}; "
        f"identity mapping: {'ON' if identity_enabled else 'off'}",
        file=sys.stderr,
    )

    # The report_facts / report_models external tables (over the normalized
    # CSVs the normalize_report_lambda lands) must exist BEFORE the views that
    # read them, so create them first.
    for stmt in report_tables_ddl(args.database, args.normalized_bucket,
                                  args.normalized_prefix):
        run_athena(athena, stmt, args.workgroup, args.database)
    print("Created report_facts / report_models external tables.", file=sys.stderr)

    if args.tables_only:
        # Pre-step (deploy.sh): create just the report tables so the
        # identity-map Lambda can read report_facts for its distinct-user
        # query, BEFORE the full view build (which needs identity_map to
        # already exist). The full invocation runs again without this flag.
        return 0

    # The identity_map external table must exist BEFORE the views that join it,
    # so create it next when enabled.
    if identity_enabled:
        for stmt in identity_map_ddl(args.database, args.identity_map_bucket,
                                     args.identity_map_prefix):
            run_athena(athena, stmt, args.workgroup, args.database)
        print("Created identity_map external table.", file=sys.stderr)

    label_parts = render_identity_label_parts(args.database, email_expr, identity_enabled)

    for path in sorted(VIEWS_DIR.glob("*.sql")):
        sql = string.Template(path.read_text()).substitute(
            database=args.database,
            email_expr=email_expr,
            base_user_label=label_parts["base_user_label"],
            base_identity_join=label_parts["base_identity_join"],
            dim_user_label=label_parts["dim_user_label"],
            dim_identity_join=label_parts["dim_identity_join"],
            dim_idc_username=label_parts["dim_idc_username"],
            dim_idc_email=label_parts["dim_idc_email"],
            base_idc_username=label_parts["base_idc_username"],
            base_idc_email=label_parts["base_idc_email"],
            # Tier display labels, rendered from one shared mapping so the four
            # sites that expose a tier cannot drift apart. Each variant differs
            # only in the raw expression it wraps:
            #   *_row  - the per-row tier column
            #   *_user - the per-user CURRENT tier (most recent activity day)
            tier_label_row=tier_label_expr("subscription_tier"),
            tier_label_user=tier_label_expr(
                "max_by(COALESCE(subscription_tier, ''), date)"
                " OVER (PARTITION BY userid)"
            ),
            tier_label_dim=tier_label_expr("subscription_tier_raw"),
            tier_label_model=tier_label_expr("m.subscription_tier"),
        )
        print(f"Applying {path.name}", file=sys.stderr)
        for statement in split_statements(sql):
            run_athena(athena, statement, args.workgroup, args.database)

    return 0


if __name__ == "__main__":
    sys.exit(main())
