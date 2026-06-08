#!/usr/bin/env python3
"""
Identity-map refresh Lambda for the Kiro User Analytics dashboard.

Resolves the opaque `user_id` GUIDs in the Kiro User Activity Report to human
identities (display name / username / email) by joining against AWS IAM
Identity Center, and lands the result as a small CSV the Athena `identity_map`
view reads.

Why this shape (see the design notes in the repo):
  * The report `user_id` IS the Identity Center *Identity Store* UserId, so the
    mapping is a direct keyed lookup - no fuzzy matching.
  * We page `identitystore:ListUsers` (throughput-safe: ~100 users/page at the
    20 TPS per-store limit, so even a 200k-user directory is ~100s) rather than
    calling DescribeUser per active user (which fans out and risks the 15-min
    Lambda timeout at scale).
  * We persist ONLY the users that actually appear in the activity data
    (intersection with `SELECT DISTINCT user_id`), so the CSV / Glue table /
    SPICE never hold the customer's whole corporate directory - only active
    Kiro users. Data minimization.
  * Fail-closed: on ANY error we leave the PREVIOUS CSV in place rather than
    overwrite it with empty/partial output (a LEFT JOIN against an empty map
    would silently drop every name and regress the dashboard).

Environment variables (set by cfn/03-identity-mapping.yaml):
  IDENTITY_STORE_ID   d-xxxxxxxxxx  (the Identity Store / directory id)
  IDC_REGION          region where Identity Center lives (may differ from the
                      dashboard region)
  IDC_ROLE_ARN        optional; assume this role for cross-account Identity
                      Center (org management / delegated admin account)
  ATHENA_DATABASE     Glue database holding base_user_activity
  ATHENA_WORKGROUP    Athena workgroup to run the distinct-user query in
  ATHENA_REGION       region of the Athena workgroup / data (the dashboard
                      region)
  MAP_BUCKET          dedicated PII bucket for the identity-map CSV
  MAP_KEY             object key for the CSV (e.g. identity-map/users.csv)
"""
from __future__ import annotations

import csv
import io
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Retry/backoff handled by botocore's adaptive mode so a transient
# Identity Store throttle (429) doesn't lose a day's refresh.
_BOTO_CFG = Config(retries={"max_attempts": 8, "mode": "adaptive"})


def _idc_client():
    """identitystore client, in IDC_REGION, optionally via a cross-account
    assume-role when Identity Center lives in another account."""
    region = os.environ["IDC_REGION"]
    role_arn = os.environ.get("IDC_ROLE_ARN", "").strip()
    if not role_arn:
        return boto3.client("identitystore", region_name=region, config=_BOTO_CFG)
    sts = boto3.client("sts", config=_BOTO_CFG)
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="kiro-identity-map",
    )["Credentials"]
    return boto3.client(
        "identitystore",
        region_name=region,
        config=_BOTO_CFG,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def active_user_ids() -> set[str]:
    """Distinct user ids that actually appear in the activity data. We only
    resolve/persist these, never the whole directory.

    We query the `report_facts` table (column `userid`), NOT the curated
    `base_user_activity` view, on purpose: build_views.py needs the identity_map
    table to exist before it can render the views, while this Lambda needs the
    user ids before that full build runs. deploy.sh creates report_facts first
    (build_views --tables-only) so this query works, breaking the ordering
    cycle. The report_facts `userid` set is identical to base_user_activity's
    distinct user_ids - dedup never drops a user, only collapses rows."""
    athena = boto3.client("athena", region_name=os.environ["ATHENA_REGION"],
                          config=_BOTO_CFG)
    database = os.environ["ATHENA_DATABASE"]
    workgroup = os.environ["ATHENA_WORKGROUP"]
    # trim(both '"' ...): defensive de-quoting. report_facts is read via
    # OpenCSVSerDe (quoteChar '"'), which normally strips wrapping quotes, but
    # some source rows have historically stored userid as "<guid>" (length 38,
    # not 36). The Identity Store UserId is the bare guid, so we strip any
    # wrapping quotes to match. trim is a no-op on unquoted values, so it's safe
    # either way. The view-side JOIN applies the identical normalisation.
    # nosec B608 - `database` is the ATHENA_DATABASE env var set by
    # cfn/03-identity-mapping.yaml from the validated DatabaseName CFN param
    # (AllowedPattern [a-z][a-z0-9_]{0,62}); not user-supplied. Athena DDL/DML
    # has no parameter binding so an f-string is the only option.
    query = f"SELECT DISTINCT trim(both '\"' from userid) FROM {database}.report_facts"  # nosec B608
    qid = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        WorkGroup=workgroup,
    )["QueryExecutionId"]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = st["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state != "SUCCEEDED":
        reason = st.get("StateChangeReason", "")
        # Table-not-found / no data yet on a brand-new deployment: treat as a
        # clean "no active users" rather than an error, so the Lambda no-ops
        # instead of failing closed with a scary message.
        if "does not exist" in reason or "not found" in reason.lower():
            return set()
        raise RuntimeError(f"Athena distinct-user query {qid} {state}: {reason}")
    ids: set[str] = set()
    paginator = athena.get_paginator("get_query_results")
    first_row = True
    for page in paginator.paginate(QueryExecutionId=qid):
        for row in page["ResultSet"]["Rows"]:
            if first_row:  # header row
                first_row = False
                continue
            cells = row.get("Data", [])
            if cells and "VarCharValue" in cells[0]:
                ids.add(cells[0]["VarCharValue"])
    return ids


def list_directory_users(idc) -> dict[str, dict]:
    """All users in the Identity Store, keyed by UserId. Held only in memory;
    we never persist the full set."""
    store_id = os.environ["IDENTITY_STORE_ID"]
    users: dict[str, dict] = {}
    paginator = idc.get_paginator("list_users")
    for page in paginator.paginate(IdentityStoreId=store_id):
        for u in page.get("Users", []):
            emails = u.get("Emails", []) or []
            primary = next((e["Value"] for e in emails if e.get("Primary")), "")
            if not primary and emails:
                primary = emails[0].get("Value", "")
            users[u["UserId"]] = {
                "username": u.get("UserName", ""),
                "display_name": u.get("DisplayName", ""),
                "email": primary,
            }
    return users


def build_csv(active: set[str], directory: dict[str, dict]) -> str:
    """Quoted CSV of ONLY the active users that resolved. Quoted because
    display names contain commas ('Doe, John'). Header row included; the
    Athena table is configured with skip.header.line.count=1."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(["user_id", "username", "display_name", "email"])
    for uid in sorted(active):
        info = directory.get(uid)
        if info is None:
            # Active in Kiro but not in the Identity Store (external IdP /
            # social / Builder ID, or removed from the directory). Skip - the
            # LEFT JOIN falls back to the UUID for these.
            continue
        w.writerow([uid, info["username"], info["display_name"], info["email"]])
    return buf.getvalue()


def handler(event, context):  # noqa: ARG001 - Lambda signature
    bucket = os.environ["MAP_BUCKET"]
    key = os.environ["MAP_KEY"]
    s3 = boto3.client("s3", config=_BOTO_CFG)

    try:
        active = active_user_ids()
        if not active:
            # No activity data yet (brand-new deployment). Leave whatever CSV
            # exists (likely the deploy-time seed) untouched - do not overwrite.
            print("No active users found; leaving existing identity map untouched.")
            return {"status": "noop", "reason": "no active users"}

        idc = _idc_client()
        directory = list_directory_users(idc)
        body = build_csv(active, directory)
        resolved = body.count("\n") - 1  # minus header
        # Single atomic PutObject - SPICE/Athena never see a half-written file.
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="text/csv",
        )
        # Keep stdout generic - resolved/active/directory counts are
        # organizational metadata, so we don't write them to the log stream.
        # The counts are still returned to the caller for the deploy summary.
        print("Identity map refresh completed successfully.")
        return {"status": "ok", "active": len(active), "resolved": resolved}
    except (ClientError, RuntimeError) as e:
        # FAIL CLOSED: do not overwrite the previous good CSV with empty/partial
        # output. Raise so the failure is visible in logs/metrics, but the
        # existing map (and therefore the dashboard's names) stays intact.
        # Log only the exception TYPE, not the full exception object, which
        # could carry user ids / directory metadata / raw API responses.
        print(f"Identity map refresh FAILED ({type(e).__name__}), "
              f"leaving previous map in place. See the traceback below.")
        raise
