#!/usr/bin/env bash
# Tear down everything created by deploy.sh.
#
# Order:
#   0. Delete the identity-map stack (optional) + purge its PII bucket.
#   1. Delete the QS Dashboard and Analysis (boto3-managed, not in CFN).
#   2. Delete the QS stack - datasets and datasource.
#   3. Empty + delete the Athena results bucket (Retain in the data stack).
#   4. Force-delete the Athena workgroup (CFN can't drop it while non-empty).
#   5. Delete the data stack - AWS Glue database, Athena workgroup, normalizer Lambda.
#   6. Revoke the buckets from the QS service role's S3 policy.
#
# Identity mapping (step 0) is removed FIRST so its daily refresh Lambda stops
# touching the data layer before we delete the rest. Its dedicated PII bucket
# (Retain) is emptied of all versions and deleted so no resolved names linger.
#
# Idempotent: skips anything already gone.

set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_PREFIX="${STACK_PREFIX:-kiro-analytics}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET:-}"
# Accept name, ARN, or s3:// URI - the bucket name is what gets passed to the
# QS S3 inline-policy revoker.
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET#arn:aws:s3:::}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET#s3://}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET%/}"
QS_IAM_ROLE_NAME="${QS_IAM_ROLE_NAME:-aws-quicksight-service-role-v0}"
DATA_STACK="${STACK_PREFIX}-data"
QS_STACK="${STACK_PREFIX}-qs"
IDMAP_STACK="${STACK_PREFIX}-identity-map"
ASSET_ID="${ASSET_ID:-${STACK_PREFIX}}"
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [[ -z "${ACCOUNT_ID}" ]]; then
    echo "Could not resolve AWS identity. Stopping." >&2
    exit 1
fi

echo ">> Tearing down account=${ACCOUNT_ID} region=${REGION} prefix=${STACK_PREFIX}"

# 0) Identity-map stack (optional). Remove FIRST so its daily refresh Lambda
# stops touching the data layer, and purge its PII bucket so no resolved
# names survive teardown.
IDMAP_BUCKET=""
echo ">> [0/6] Removing identity-map stack ${IDMAP_STACK} (if present)"
if aws cloudformation describe-stacks --stack-name "${IDMAP_STACK}" --region "${REGION}" >/dev/null 2>&1; then
    # Recover the PII bucket name before deleting the stack (the bucket has
    # DeletionPolicy: Retain, so we must empty + delete it ourselves).
    IDMAP_BUCKET="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${IDMAP_STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='IdentityMapBucketName'].OutputValue" --output text 2>/dev/null || echo "")"
    IDMAP_LOG_BUCKET="${STACK_PREFIX}-idmap-logs-${ACCOUNT_ID}-${REGION}"

    for b in "${IDMAP_BUCKET}" "${IDMAP_LOG_BUCKET}"; do
        [[ -z "${b}" ]] && continue
        if aws s3api head-bucket --bucket "${b}" --region "${REGION}" >/dev/null 2>&1; then
            echo "   purging all object versions in ${b}"
            python3 - "${b}" "${REGION}" <<'PY'
import sys, boto3
bucket, region = sys.argv[1], sys.argv[2]
s3 = boto3.client("s3", region_name=region)
paginator = s3.get_paginator("list_object_versions")
to_delete = []
for page in paginator.paginate(Bucket=bucket):
    for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
        to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        if len(to_delete) == 1000:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
            to_delete = []
if to_delete:
    s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
PY
            aws s3api delete-bucket --bucket "${b}" --region "${REGION}" >/dev/null 2>&1 \
                && echo "   ${b} deleted" \
                || echo "   ${b}: delete deferred to stack (may be stack-managed)"
        fi
    done

    aws cloudformation delete-stack --stack-name "${IDMAP_STACK}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${IDMAP_STACK}" --region "${REGION}" \
        && echo "   stack deleted" \
        || echo "   stack delete still settling; check for a Retain'd KMS key/bucket" >&2
    # The KMS key has DeletionPolicy: Retain (it's a CMK). It is orphaned, not
    # billed beyond the key itself; schedule its deletion manually if desired:
    echo "   note: the identity-map KMS key is retained (alias/${STACK_PREFIX}-identity-map);"
    echo "         schedule-key-deletion manually if you want it gone."
else
    echo "   not present"
fi

# 1) QuickSight assets owned outside CFN
echo ">> [1/6] Deleting QS Dashboard + Analysis"
if aws quicksight describe-dashboard --aws-account-id "${ACCOUNT_ID}" \
        --dashboard-id "${ASSET_ID}" --region "${REGION}" >/dev/null 2>&1; then
    aws quicksight delete-dashboard --aws-account-id "${ACCOUNT_ID}" \
        --dashboard-id "${ASSET_ID}" --region "${REGION}" >/dev/null
    echo "   dashboard deleted"
else
    echo "   dashboard already gone"
fi

if aws quicksight describe-analysis --aws-account-id "${ACCOUNT_ID}" \
        --analysis-id "${ASSET_ID}" --region "${REGION}" >/dev/null 2>&1; then
    aws quicksight delete-analysis --aws-account-id "${ACCOUNT_ID}" \
        --analysis-id "${ASSET_ID}" --region "${REGION}" \
        --force-delete-without-recovery >/dev/null
    echo "   analysis deleted"
else
    echo "   analysis already gone"
fi

# 2) QS stack - datasets and datasource
echo ">> [2/6] Deleting ${QS_STACK}"
if aws cloudformation describe-stacks --stack-name "${QS_STACK}" --region "${REGION}" >/dev/null 2>&1; then
    aws cloudformation delete-stack --stack-name "${QS_STACK}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${QS_STACK}" --region "${REGION}"
    echo "   stack deleted"
else
    echo "   stack already gone"
fi

# 3) Empty + delete the Athena results bucket. The bucket has versioning
# enabled, so `aws s3 rm` alone leaves delete-markers and previous versions
# behind - delete every version + delete-marker via boto3 before
# delete-bucket.
RESULTS_BUCKET="${STACK_PREFIX}-data-athena-results-${ACCOUNT_ID}-${REGION}"

echo ">> [3/6] Deleting Athena results bucket ${RESULTS_BUCKET}"
if aws s3api head-bucket --bucket "${RESULTS_BUCKET}" --region "${REGION}" >/dev/null 2>&1; then
    python3 - "${RESULTS_BUCKET}" "${REGION}" <<'PY'
import sys, boto3
bucket, region = sys.argv[1], sys.argv[2]
s3 = boto3.client("s3", region_name=region)
paginator = s3.get_paginator("list_object_versions")
to_delete = []
for page in paginator.paginate(Bucket=bucket):
    for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
        to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        if len(to_delete) == 1000:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
            to_delete = []
if to_delete:
    s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
PY
    aws s3api delete-bucket --bucket "${RESULTS_BUCKET}" --region "${REGION}"
    echo "   bucket deleted"
else
    echo "   bucket already gone"
fi

# 4) Athena workgroup - CFN refuses to delete it while query history exists,
# so we force-delete with --recursive-delete-option before the data stack.
WORKGROUP="${STACK_PREFIX}"
WORKGROUP="${WORKGROUP//-/_}"   # CFN default WorkgroupName is kiro_analytics
echo ">> [4/6] Force-deleting Athena workgroup ${WORKGROUP} (clears query history)"
if aws athena get-work-group --work-group "${WORKGROUP}" --region "${REGION}" >/dev/null 2>&1; then
    aws athena delete-work-group --work-group "${WORKGROUP}" \
        --recursive-delete-option --region "${REGION}" >/dev/null && \
        echo "   workgroup deleted" || \
        echo "   workgroup delete failed (will retry via stack)"
else
    echo "   workgroup already gone"
fi

# 5) Data stack
echo ">> [5/6] Deleting ${DATA_STACK}"
if aws cloudformation describe-stacks --stack-name "${DATA_STACK}" --region "${REGION}" >/dev/null 2>&1; then
    aws cloudformation delete-stack --stack-name "${DATA_STACK}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${DATA_STACK}" --region "${REGION}"
    echo "   stack deleted"
else
    echo "   stack already gone"
fi

# 6) Revoke buckets from the QS service role's S3 policy. Skipped silently
# if the role/policy isn't present (e.g. custom QS service role).
echo ">> [6/6] Revoking buckets from QS S3 policy"
REVOKE_BUCKETS=("${RESULTS_BUCKET}")
if [[ -n "${KIRO_LOGS_BUCKET}" ]]; then
    REVOKE_BUCKETS+=("${KIRO_LOGS_BUCKET}")
fi
python3 "${ROOT}/scripts/grant_quicksight_s3.py" --revoke \
    --role-name "${QS_IAM_ROLE_NAME}" \
    --region "${REGION}" \
    --buckets "${REVOKE_BUCKETS[@]}" 2>&1 | sed 's/^/   /' || \
    echo "   skipped (custom QS service role or policy not found)"

echo
echo ">> Done. Untouched: your Kiro logs S3 bucket and any data inside it."
