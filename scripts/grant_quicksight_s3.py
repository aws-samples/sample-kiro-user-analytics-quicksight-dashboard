#!/usr/bin/env python3
"""
Grant Amazon QuickSight access to the buckets this dashboard reads and writes.

We attach an *inline* IAM policy named `KiroAnalyticsQuickSightS3Access` to
the QuickSight service role `aws-quicksight-service-role-v0`. This is
strictly additive - IAM unions all attached policies on a role, so the
buckets QS already has access to via the console-managed AWSQuickSightS3Policy
keep working. We do not modify AWSQuickSightS3Policy itself; the QuickSight
console retains full ownership of it.

Two access modes per bucket:
    read         - needed for the Kiro logs bucket (source data).
    read_write   - needed for the Athena results bucket. QuickSight runs
                   Athena queries from inside QS, and Athena writes results
                   into this bucket; QS therefore needs PutObject and the
                   multipart-upload actions on it. Without write access, the
                   AthenaDataSource connection test fails with "Unable to
                   verify/create output bucket".

References:
    https://repost.aws/knowledge-center/quicksight-permission-errors
    https://repost.aws/knowledge-center/athena-output-bucket-error

Exit codes:
    0  inline policy already matches the requested spec, or --apply succeeded
    1  policy needs updating but --apply not passed (plan printed)
    2  unrecoverable error (no QS service role, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_ROLE_NAME = "aws-quicksight-service-role-v0"  # the QS-managed default
INLINE_POLICY_NAME = "KiroAnalyticsQuickSightS3Access"

READ_BUCKET_ACTIONS  = ["s3:ListBucket", "s3:GetBucketLocation"]
READ_OBJECT_ACTIONS  = ["s3:GetObject", "s3:GetObjectVersion"]
WRITE_OBJECT_ACTIONS = ["s3:PutObject", "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts"]
# kms:Decrypt only - QuickSight/Athena reads (GETs) the SSE-KMS identity-map
# CSV; it never writes it, so no Encrypt/GenerateDataKey. The CMK's key policy
# delegates access control to IAM (account-root statement), so granting Decrypt
# on this role's inline policy is sufficient and stays least-privilege.
KMS_READ_ACTIONS = ["kms:Decrypt"]

MODES = {
    "read":       {"bucket": READ_BUCKET_ACTIONS,
                   "object": READ_OBJECT_ACTIONS},
    "read_write": {"bucket": READ_BUCKET_ACTIONS,
                   "object": READ_OBJECT_ACTIONS + WRITE_OBJECT_ACTIONS},
}


def parse_bucket_specs(specs: list[str]) -> list[tuple[str, str]]:
    """Parse `name:mode` pairs (mode optional, defaults to `read`)."""
    out = []
    for s in specs:
        name, _, mode = s.partition(":")
        mode = mode or "read"
        if mode not in MODES:
            raise SystemExit(f"Unknown access mode {mode!r} for bucket {name!r}. "
                             f"Use one of: {', '.join(MODES)}.")
        out.append((name, mode))
    return out


def render_policy(bucket_specs: list[tuple[str, str]],
                  kms_key_arns: list[str] | None = None) -> dict:
    """Build the inline policy document deterministically from the spec.
    Mirrors what QuickSight's console writes: a top-level ListAllMyBuckets
    statement, then one pair of statements per bucket (bucket-level +
    object-level). If kms_key_arns is given, append one kms:Decrypt statement
    so QuickSight/Athena can read SSE-KMS objects (the identity-map CSV)."""
    statements = [
        # QuickSight's console adds this whenever any bucket is selected;
        # we mirror it for parity so the data-source picker can list all
        # buckets the QS user can see.
        #
        # The "arn:aws:s3:::*" wildcard is REQUIRED, not lax IAM: the
        # s3:ListAllMyBuckets action does not support resource-scoped ARNs
        # at the AWS API level - "*" is the only valid Resource for it. It
        # grants the ability to enumerate bucket *names* only, never object
        # contents (that's gated by the per-bucket Read/ReadWrite statements
        # below). This mirrors the policy the QuickSight console itself
        # writes. See SECURITY.md "AWS IAM" for the full rationale.
        {
            "Sid": "ListAllBuckets",
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "arn:aws:s3:::*",
        },
    ]
    for bucket, mode in bucket_specs:
        actions = MODES[mode]
        sid_prefix = "Read" if mode == "read" else "ReadWrite"
        # Statement Sid must be alphanumeric - strip anything else from the
        # bucket name so the Sid is valid.
        bucket_token = "".join(c for c in bucket if c.isalnum())[:64] or "Bucket"
        statements.append({
            "Sid": f"{sid_prefix}{bucket_token}Bucket",
            "Effect": "Allow",
            "Action": actions["bucket"],
            "Resource": f"arn:aws:s3:::{bucket}",
        })
        statements.append({
            "Sid": f"{sid_prefix}{bucket_token}Objects",
            "Effect": "Allow",
            "Action": actions["object"],
            "Resource": f"arn:aws:s3:::{bucket}/*",
        })
    for i, key_arn in enumerate(kms_key_arns or []):
        statements.append({
            "Sid": f"DecryptKmsKey{i}",
            "Effect": "Allow",
            "Action": KMS_READ_ACTIONS,
            "Resource": key_arn,
        })
    return {"Version": "2012-10-17", "Statement": statements}


def get_inline_policy(iam, role_name: str, policy_name: str) -> dict | None:
    try:
        resp = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            return None
        raise
    return resp["PolicyDocument"]


def _allowed_actions_per_resource(doc: dict) -> dict[str, set[str]]:
    """Flatten an Allow-only policy into {resource_arn: {actions}}. Only
    Allow statements are considered - explicit Deny would change semantics
    but isn't something we render in this policy."""
    result: dict[str, set[str]] = {}
    for stmt in doc.get("Statement", []):
        if stmt.get("Effect") != "Allow":
            continue
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        resources = stmt.get("Resource", [])
        if isinstance(resources, str):
            resources = [resources]
        for r in resources:
            result.setdefault(r, set()).update(actions)
    return result


def policy_covers(current: dict, desired: dict) -> bool:
    """True if every (resource, action) pair in `desired` is already allowed
    by `current`. Extra resources in `current` (e.g. buckets from a parallel
    deploy) are fine - we only verify our needs are met."""
    have = _allowed_actions_per_resource(current)
    want = _allowed_actions_per_resource(desired)
    for resource, needed in want.items():
        if not needed.issubset(have.get(resource, set())):
            return False
    return True


def print_plan(current: dict | None, desired: dict, region: str, role_name: str,
               bucket_specs: list[tuple[str, str]],
               kms_key_arns: list[str] | None = None) -> None:
    action = "CREATE" if current is None else "UPDATE"
    print(
        f"\n[{action}] Would write inline policy {INLINE_POLICY_NAME!r} on "
        f"{role_name}:",
        file=sys.stderr,
    )
    for bucket, mode in bucket_specs:
        print(f"  - {bucket} ({mode})", file=sys.stderr)
    for key_arn in kms_key_arns or []:
        print(f"  - {key_arn} (kms:Decrypt)", file=sys.stderr)
    spec_str = " ".join(f"{b}:{m}" for b, m in bucket_specs)
    kms_arg = "".join(f" --kms-key-arn {k}" for k in (kms_key_arns or []))
    role_arg = "" if role_name == DEFAULT_ROLE_NAME else f" --role-name {role_name}"
    print(
        "\nApply now:\n"
        f"  python3 scripts/grant_quicksight_s3.py --apply \\\n"
        f"      --region {region}{role_arg} --buckets {spec_str}{kms_arg}",
        file=sys.stderr,
    )
    print(
        "\nReferences:\n"
        "  - https://repost.aws/knowledge-center/quicksight-permission-errors\n"
        "  - https://repost.aws/knowledge-center/athena-output-bucket-error",
        file=sys.stderr,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument(
        "--buckets", nargs="+", required=True,
        help="Bucket specs of the form `name` or `name:mode`. Modes: "
             f"{', '.join(MODES)}. Default mode is `read`.",
    )
    p.add_argument(
        "--role-name", default=DEFAULT_ROLE_NAME,
        help=f"Name of the IAM role QuickSight uses. Default: "
             f"{DEFAULT_ROLE_NAME!r} (the QS-managed default). Override if "
             "your account uses an existing role under QuickSight -> Manage "
             "account -> Permissions -> IAM role -> Use an existing role.",
    )
    p.add_argument(
        "--kms-key-arn", action="append", default=[], metavar="ARN",
        help="KMS key ARN that QuickSight/Athena must be able to Decrypt "
             "(the SSE-KMS identity-map bucket's CMK). Repeatable.",
    )
    p.add_argument("--apply", action="store_true",
                   help=f"Write the inline role policy {INLINE_POLICY_NAME!r}. "
                        "Without this, the script prints the plan and exits 1.")
    p.add_argument("--revoke", action="store_true",
                   help=f"Delete the inline role policy {INLINE_POLICY_NAME!r}. "
                        "Used by teardown.")
    args = p.parse_args()

    bucket_specs = parse_bucket_specs(args.buckets)
    role_name = args.role_name
    iam = boto3.client("iam")

    try:
        iam.get_role(RoleName=role_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        if role_name == DEFAULT_ROLE_NAME:
            print(
                f"[ERROR] IAM role {role_name!r} not found.\n"
                "If QuickSight is using an existing role (QuickSight ->\n"
                "Manage account -> Permissions -> IAM role -> Use an existing role),\n"
                "set QS_IAM_ROLE_NAME to that role's name and re-run.\n"
                "Reference: https://repost.aws/knowledge-center/quicksight-permission-errors",
                file=sys.stderr,
            )
        else:
            print(f"[ERROR] IAM role {role_name!r} not found.", file=sys.stderr)
        return 2

    print(f">> QuickSight IAM role: {role_name}", file=sys.stderr)
    current = get_inline_policy(iam, role_name, INLINE_POLICY_NAME)

    if args.revoke:
        if current is None:
            print(f"[OK] Inline policy {INLINE_POLICY_NAME!r} not present on "
                  f"{role_name}; nothing to revoke.", file=sys.stderr)
            return 0
        iam.delete_role_policy(RoleName=role_name, PolicyName=INLINE_POLICY_NAME)
        print(f"[OK] Removed inline policy {INLINE_POLICY_NAME!r} from {role_name}.",
              file=sys.stderr)
        return 0

    desired = render_policy(bucket_specs, args.kms_key_arn)

    if current is not None and policy_covers(current, desired):
        print(f"[OK] Inline policy {INLINE_POLICY_NAME!r} already covers all "
              f"requested buckets and actions.", file=sys.stderr)
        return 0

    if not args.apply:
        print_plan(current, desired, args.region, role_name, bucket_specs,
                   args.kms_key_arn)
        return 1

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(desired),
    )
    print(f"[OK] {'Created' if current is None else 'Updated'} inline policy "
          f"{INLINE_POLICY_NAME!r} on {role_name}.", file=sys.stderr)
    for bucket, mode in bucket_specs:
        print(f"     {bucket} ({mode})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
