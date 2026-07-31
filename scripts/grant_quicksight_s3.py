#!/usr/bin/env python3
"""
Grant Amazon QuickSight access to the buckets this dashboard reads and writes.

We attach an *inline* IAM policy (see --policy-name) to the QuickSight service
role `aws-quicksight-service-role-v0`. This is strictly additive with respect
to OTHER policies - IAM unions all policies on a role, so the buckets QS
already has access to via the console-managed AWSQuickSightS3Policy keep
working. We do not modify AWSQuickSightS3Policy itself; the QuickSight console
retains full ownership of it.

It is NOT additive with respect to itself: --apply writes the named policy with
put_role_policy (a wholesale replace) and --revoke deletes it. Since every
deployment in an account shares the one QuickSight service role, the policy
name must be unique per deployment or one stack will strip another's grants.
deploy.sh and teardown.sh therefore pass --policy-name derived from
STACK_PREFIX.

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
import re
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_ROLE_NAME = "aws-quicksight-service-role-v0"  # the QS-managed default

# Default inline-policy name. Callers SHOULD pass --policy-name derived from
# their STACK_PREFIX so two deployments in one account never share a policy.
#
# Why that matters: this script writes the policy with put_role_policy, which
# REPLACES the named policy wholesale, and --revoke deletes it outright. All
# deployments in an account share one QuickSight service role, so a single
# fixed name means deploying stack B silently strips stack A's bucket grants -
# stack A then fails its next SPICE refresh with "Unable to verify/create
# output bucket" or a PERMISSION_DENIED on s3:ListBucket. Observed in practice
# with two deployments in one account. Namespacing by stack prefix keeps each
# deployment's grants independent.
DEFAULT_INLINE_POLICY_NAME = "KiroAnalyticsQuickSightS3Access"

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


# Bucket-name syntax (same rule as build_views._S3_NAME_RE): lower-case
# alphanumeric, dots and hyphens, 3-63 chars, must start and end alphanumeric.
# Validated HERE because this function is the single choke point every caller
# passes through, and because the name is interpolated straight into an IAM
# Resource ARN below. Without it, KIRO_LOGS_BUCKET='*' (or 's3://*', which a
# shell can produce from an unquoted glob) rendered
#   arn:aws:s3:::*  +  arn:aws:s3:::*/*
# granting the QuickSight service role GetObject on EVERY object in the account.
# That grant is worse than a deploy-time mistake: it lands on a role assumed by
# QuickSight rather than by the deployer, it persists after the deploy exits,
# and it is shared by every QuickSight principal in the account.
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


def parse_bucket_specs(specs: list[str]) -> list[tuple[str, str]]:
    """Parse `name:mode` pairs (mode optional, defaults to `read`).

    Rejects anything that is not a syntactically valid S3 bucket name, so a
    wildcard or a typo can never widen the rendered IAM policy.
    """
    out = []
    for s in specs:
        name, _, mode = s.partition(":")
        mode = mode or "read"
        if mode not in MODES:
            raise SystemExit(f"Unknown access mode {mode!r} for bucket {name!r}. "
                             f"Use one of: {', '.join(MODES)}.")
        if not _BUCKET_NAME_RE.match(name):
            raise SystemExit(
                f"Refusing to build an IAM policy for {name!r}: not a valid S3 "
                f"bucket name. Expected 3-63 lower-case alphanumeric characters, "
                f"dots or hyphens, starting and ending alphanumeric. A wildcard "
                f"or malformed name here would grant QuickSight access to buckets "
                f"you did not intend."
            )
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
               kms_key_arns: list[str] | None = None,
               policy_name: str = DEFAULT_INLINE_POLICY_NAME) -> None:
    action = "CREATE" if current is None else "UPDATE"
    print(
        f"\n[{action}] Would write inline policy {policy_name!r} on "
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
    # Echo --policy-name whenever it is non-default, so the copy-pasteable
    # command writes the SAME policy this plan describes (pasting it without
    # the flag would write the default-named policy instead).
    policy_arg = ("" if policy_name == DEFAULT_INLINE_POLICY_NAME
                  else f" --policy-name {policy_name}")
    print(
        "\nApply now:\n"
        f"  python3 scripts/grant_quicksight_s3.py --apply \\\n"
        f"      --region {region}{role_arg}{policy_arg} --buckets {spec_str}{kms_arg}",
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
    p.add_argument(
        "--policy-name", default=DEFAULT_INLINE_POLICY_NAME,
        help="Name of the inline policy to write on the role. Default: "
             f"{DEFAULT_INLINE_POLICY_NAME!r}. deploy.sh/teardown.sh pass a "
             "name derived from STACK_PREFIX so parallel deployments in one "
             "account do not overwrite each other's grants (this policy is "
             "written with put_role_policy, which replaces it wholesale).",
    )
    p.add_argument("--apply", action="store_true",
                   help="Write the inline role policy (see --policy-name). "
                        "Without this, the script prints the plan and exits 1.")
    p.add_argument("--revoke", action="store_true",
                   help="Delete the inline role policy (see --policy-name). "
                        "Used by teardown. NOTE: this removes the ENTIRE named "
                        "policy; --buckets is ignored for revoke.")
    args = p.parse_args()
    policy_name = args.policy_name

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
    current = get_inline_policy(iam, role_name, policy_name)

    if args.revoke:
        if current is None:
            print(f"[OK] Inline policy {policy_name!r} not present on "
                  f"{role_name}; nothing to revoke.", file=sys.stderr)
            return 0
        iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
        print(f"[OK] Removed inline policy {policy_name!r} from {role_name}.",
              file=sys.stderr)
        return 0

    desired = render_policy(bucket_specs, args.kms_key_arn)

    if current is not None and policy_covers(current, desired):
        print(f"[OK] Inline policy {policy_name!r} already covers all "
              f"requested buckets and actions.", file=sys.stderr)
        return 0

    if not args.apply:
        print_plan(current, desired, args.region, role_name, bucket_specs,
                   args.kms_key_arn, policy_name)
        return 1

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(desired),
    )
    print(f"[OK] {'Created' if current is None else 'Updated'} inline policy "
          f"{policy_name!r} on {role_name}.", file=sys.stderr)
    for bucket, mode in bucket_specs:
        print(f"     {bucket} ({mode})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
