#!/usr/bin/env python3
"""
Render and execute the Athena views in athena/.

Most views are static and only need ${database} substituted. The model_usage view contains a
${model_case_expression} placeholder that is filled at deploy time by
inspecting the live raw_user_report table - Athena cannot dereference column
names dynamically, so the unpivot is generated as a CASE chain.

Usage:
    python scripts/build_views.py \\
        --database kiro_analytics \\
        --workgroup kiro_analytics \\
        --region us-east-1

Requires AWS credentials with athena:StartQueryExecution and
glue:GetTable on the database.
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


def get_columns(glue, database: str, table: str) -> list[str]:
    resp = glue.get_table(DatabaseName=database, Name=table)
    return [c["Name"] for c in resp["Table"]["StorageDescriptor"]["Columns"]]


def list_model_columns(columns: list[str]) -> list[str]:
    """Per-model columns: end with '_messages', exclude 'total_messages'."""
    return [
        c for c in columns
        if c.lower().endswith("_messages") and c.lower() != "total_messages"
    ]


def email_expression(columns: list[str], hash_emails: bool) -> str:
    """If raw_user_report has an `email` column, reference it; otherwise
    fall back to NULL so the view compiles. Column lookup is case-insensitive
    because the crawler can preserve mixed case.

    If hash_emails is True, wrap the column in to_hex(sha256(...)) so PII
    does not reach SPICE. The view layer then sees opaque hex digests.
    """
    column = None
    for c in columns:
        if c.lower() == "email":
            column = c
            break
    if column is None:
        return "CAST(NULL AS varchar)"
    if hash_emails:
        return f"to_hex(sha256(CAST({column} AS varbinary)))"
    return column


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


def render_model_union(
    database: str,
    model_columns: list[str],
    email_expr: str,
    label_expr: str,
    identity_join: str,
) -> str:
    """Build a UNION ALL of one SELECT per discovered <model>_messages column.
    Each branch unpivots that column into (model_name, messages) rows. Each
    row also carries user_label so the drill-by-user filter on the User-detail
    sheet has a column to match on - `label_expr` is the exact same expression
    rendered into base_user_activity / user_dim (with or without the identity
    map), so the three views always agree on a user's label.

    `identity_join` is "" normally, or a LEFT JOIN onto the identity_map table
    when identity mapping is enabled (see render_identity_label_parts).

    Security note: this function constructs SQL by string interpolation
    because Athena does not support parameterized DDL. Every interpolated
    value originates from a trusted source - `database` is a CFN parameter,
    `model_columns` are column names returned by AWS Glue get-table,
    `email_expr` is one of two literal strings we construct ourselves
    (the column ref or `to_hex(sha256(...))`), and `label_expr`/`identity_join`
    are built from `database` + literals here. We additionally re-validate
    each identifier with `_validate_identifier` before interpolation.
    Bandit B608 is therefore a known false positive for this construction;
    see the `# nosec` annotation on the f-string below."""
    _validate_identifier(database, "database")
    # subscription_tier carried on every model_usage row so the Tier picker can
    # filter the model visuals (Daily messages by model, Top users by model,
    # Model split). Normalized identically to base_user_activity / user_dim so
    # the picker's values match across datasets.
    tier_expr = (
        "CASE upper(COALESCE(subscription_tier, ''))"
        " WHEN 'PRO' THEN 'Pro'"
        " WHEN 'PRO_PLUS' THEN 'Pro+'"
        " WHEN 'POWER' THEN 'Power'"
        " WHEN '' THEN 'Unknown'"
        " ELSE subscription_tier END"
    )
    if not model_columns:
        return (
            "SELECT\n"
            "    CAST(NULL AS date)    AS activity_date,\n"
            "    CAST(NULL AS varchar) AS user_id,\n"
            "    CAST(NULL AS varchar) AS user_label,\n"
            "    CAST(NULL AS varchar) AS client_type,\n"
            "    CAST(NULL AS varchar) AS subscription_tier,\n"
            "    CAST(NULL AS varchar) AS model_name,\n"
            "    CAST(NULL AS bigint)  AS messages\n"
            "WHERE 1 = 0"
        )
    branches = []
    for name in model_columns:
        _validate_identifier(name, "model column")
        model_name = name[:-len("_messages")] if name.lower().endswith("_messages") else name
        # Quote the column ref because real-world model columns can contain
        # characters that break Athena's identifier parser, e.g.
        # claude_opus_4.6_messages where the '.6' is read as a number literal.
        col = f'"{name}"'
        join_clause = f"\n{identity_join}" if identity_join else ""
        # nosec B608 - identifiers validated by _validate_identifier above;
        # Athena DDL has no parameter binding so f-string is the only option.
        branch_sql = (
            f"SELECT\n"  # nosec B608
            f"    CAST(date AS date)                              AS activity_date,\n"
            f"    userid                                          AS user_id,\n"
            f"    {label_expr}                                    AS user_label,\n"
            f"    upper(client_type)                              AS client_type,\n"
            f"    {tier_expr}                                     AS subscription_tier,\n"
            f"    '{model_name}'                                  AS model_name,\n"
            f"    COALESCE(TRY(CAST({col} AS bigint)), 0)         AS messages\n"
            f"FROM {database}.raw_user_report{join_clause}\n"
            f"WHERE COALESCE(TRY(CAST({col} AS bigint)), 0) > 0"
        )
        branches.append(branch_sql)
    return "\nUNION ALL\n".join(branches)


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


def render_identity_label_parts(database: str, email_expr: str, enabled: bool) -> dict[str, str]:
    """Build the template substitutions that switch user_label between the
    plain (email-or-uuid) and identity-resolved forms, plus the LEFT JOIN
    clauses each view needs. The same precedence
    (display_name -> email -> username -> uuid) is rendered into all three
    label sites so they stay consistent.

    Returned keys:
      base_user_label / base_identity_join  - for base_user_activity (raw cols)
      dim_user_label  / dim_identity_join   - for user_dim (email_per_user CTE)
    (model_usage uses base_user_label + base_identity_join via render_model_union.)
    """
    if not enabled:
        return {
            "base_user_label": f"COALESCE(NULLIF({email_expr}, ''), userid)",
            "base_identity_join": "",
            "dim_user_label": "COALESCE(NULLIF(email, ''), user_id)",
            "dim_identity_join": "",
        }
    _validate_identifier(database, "database")
    # The crawler's SerDe can keep the source CSV's surrounding double-quotes
    # in the userid value (observed: "<guid>", length 38 not 36), while the
    # Identity Store UserId (idc_user_id) is the bare guid. We therefore strip
    # any wrapping quotes from the report side of the JOIN with
    # trim(both '"' ...) - a no-op when the value isn't quoted, so it's safe
    # for both shapes. The Lambda's distinct-user query applies the identical
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
            f"LEFT JOIN {database}.identity_map im "
            f"ON im.idc_user_id = trim(both '\"' from userid)"
        ),
        "dim_user_label": (
            "COALESCE(NULLIF(im.idc_display_name, ''), NULLIF(email, ''), "
            "NULLIF(im.idc_username, ''), user_id)"
        ),
        "dim_identity_join": (
            f"LEFT JOIN {database}.identity_map im "
            f"ON im.idc_user_id = trim(both '\"' from user_id)"
        ),
    }


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

    glue = boto3.client("glue", region_name=args.region)
    athena = boto3.client("athena", region_name=args.region)

    user_report_cols = get_columns(glue, args.database, "raw_user_report")
    model_cols = list_model_columns(user_report_cols)
    email_expr = email_expression(user_report_cols, args.hash_emails)
    print(
        f"Discovered {len(model_cols)} per-model columns; "
        f"email column: {'present' if email_expr != 'CAST(NULL AS varchar)' else 'absent'}; "
        f"identity mapping: {'ON' if identity_enabled else 'off'}",
        file=sys.stderr,
    )

    # The identity_map external table must exist BEFORE the views that join it,
    # so create it first when enabled.
    if identity_enabled:
        for stmt in identity_map_ddl(args.database, args.identity_map_bucket,
                                     args.identity_map_prefix):
            run_athena(athena, stmt, args.workgroup, args.database)
        print("Created identity_map external table.", file=sys.stderr)

    label_parts = render_identity_label_parts(args.database, email_expr, identity_enabled)
    model_union = render_model_union(
        args.database, model_cols, email_expr,
        label_parts["base_user_label"], label_parts["base_identity_join"],
    )

    for path in sorted(VIEWS_DIR.glob("*.sql")):
        sql = string.Template(path.read_text()).substitute(
            database=args.database,
            model_union=model_union,
            email_expr=email_expr,
            base_user_label=label_parts["base_user_label"],
            base_identity_join=label_parts["base_identity_join"],
            dim_user_label=label_parts["dim_user_label"],
            dim_identity_join=label_parts["dim_identity_join"],
        )
        print(f"Applying {path.name}", file=sys.stderr)
        # An Athena workgroup query can only carry one statement; split on a
        # bare ';' on its own line so we don't break SQL bodies that contain
        # semicolons inside expressions.
        for statement in [s.strip() for s in sql.split(";\n") if s.strip()]:
            run_athena(athena, statement, args.workgroup, args.database)

    return 0


if __name__ == "__main__":
    sys.exit(main())
