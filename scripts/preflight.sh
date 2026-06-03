#!/usr/bin/env bash
# Pre-deployment check for the Kiro analytics dashboard.
# Verifies prerequisites are met and prints actionable next steps.
# Exits non-zero if any blocker is found.

set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_PREFIX="${STACK_PREFIX:-kiro-analytics}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET:-}"
QS_PRINCIPAL_ARN="${QS_PRINCIPAL_ARN:-}"

# Accept name, ARN (arn:aws:s3:::name), or s3:// URI for KIRO_LOGS_BUCKET so
# users who paste the bucket ARN don't end up with a literal ARN string in
# subsequent `aws s3 ls "s3://${KIRO_LOGS_BUCKET}/"` calls.
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET#arn:aws:s3:::}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET#s3://}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET%/}"

PASS=0; WARN=0; FAIL=0
ok()   { printf "  [OK]   %s\n" "$1"; PASS=$((PASS+1)); }
warn() { printf "  [WARN] %s\n" "$1"; WARN=$((WARN+1)); }
fail() { printf "  [FAIL] %s\n" "$1"; FAIL=$((FAIL+1)); }

echo "=== Preflight check ==="

# 1) Required env vars
echo
echo "[1/6] Required environment"
[[ -n "${KIRO_LOGS_BUCKET}" ]] && ok "KIRO_LOGS_BUCKET=${KIRO_LOGS_BUCKET}" \
    || fail "KIRO_LOGS_BUCKET unset - point at the bucket Kiro exports to"
[[ -n "${QS_PRINCIPAL_ARN}" ]] && ok "QS_PRINCIPAL_ARN=${QS_PRINCIPAL_ARN}" \
    || fail "QS_PRINCIPAL_ARN unset - find via 'aws quicksight list-users --aws-account-id <id> --namespace default --region ${REGION}'"

# 2) AWS identity
echo
echo "[2/6] AWS identity"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [[ -n "${ACCOUNT_ID}" ]]; then
    ok "Caller account ${ACCOUNT_ID}, region ${REGION}"
else
    fail "Could not resolve AWS identity. Check your credentials."
fi

# 3) QuickSight Enterprise + region
echo
echo "[3/6] QuickSight subscription"
if [[ -n "${ACCOUNT_ID}" ]]; then
    QS_INFO="$(aws quicksight describe-account-subscription --aws-account-id "${ACCOUNT_ID}" \
        --region "${REGION}" --query 'AccountInfo.[Edition,AccountSubscriptionStatus]' \
        --output text 2>/dev/null || true)"
    if [[ -z "${QS_INFO}" ]]; then
        fail "QuickSight is not subscribed in ${REGION}. Sign up via the QS console first."
    else
        EDITION="$(awk '{print $1}' <<<"${QS_INFO}")"
        STATUS="$(awk '{print $2}' <<<"${QS_INFO}")"
        case "${EDITION}" in
            ENTERPRISE|ENTERPRISE_AND_Q) ok "QS edition ${EDITION}, status ${STATUS}" ;;
            STANDARD)                     fail "QS is on STANDARD edition. Upgrade to Enterprise - required for the asset APIs." ;;
            *)                            fail "QS edition ${EDITION:-unknown}, status ${STATUS:-unknown}" ;;
        esac
    fi
fi

# 3.5) QS region matches data region - Athena DataSource doesn't take a
# region override, so QS must be in the same region as the workgroup.
echo
echo "[3.5/6] QuickSight region matches data region"
QS_HOME_REGION="$(aws quicksight describe-account-settings --aws-account-id "${ACCOUNT_ID}" \
    --region "${REGION}" --query 'AccountSettings.DefaultNamespace' --output text 2>/dev/null || true)"
if [[ -n "${QS_HOME_REGION}" ]]; then
    ok "QuickSight reachable in ${REGION}"
else
    fail "QuickSight not reachable in ${REGION}. The Athena DataSource cannot cross regions; sign QS up in ${REGION} or move data to QS's region."
fi

# 4) QS user exists
echo
echo "[4/6] QuickSight principal"
if [[ -n "${QS_PRINCIPAL_ARN}" && -n "${ACCOUNT_ID}" ]]; then
    # Principal can be a user OR a group; usernames may contain '/' (e.g. IAM
    # federation produces "role/session"). list-users handles both cleanly,
    # describe-user does not - match by ARN substring instead.
    if aws quicksight list-users --aws-account-id "${ACCOUNT_ID}" --namespace default \
            --region "${REGION}" --query 'UserList[].Arn' --output text 2>/dev/null \
            | tr '\t' '\n' | grep -Fxq "${QS_PRINCIPAL_ARN}"; then
        ok "Principal exists in QS namespace 'default'"
    elif aws quicksight list-groups --aws-account-id "${ACCOUNT_ID}" --namespace default \
            --region "${REGION}" --query 'GroupList[].Arn' --output text 2>/dev/null \
            | tr '\t' '\n' | grep -Fxq "${QS_PRINCIPAL_ARN}"; then
        ok "Principal exists in QS namespace 'default' (group)"
    else
        fail "Principal not found. List with 'aws quicksight list-users --aws-account-id ${ACCOUNT_ID} --namespace default --region ${REGION}'"
    fi
fi

# 5) Kiro logs are landing in the bucket. We avoid `aws s3 ls --recursive`
# because on large buckets that hold other content it paginates through
# every key. If KIRO_LOGS_PREFIX is set we probe that exact location;
# otherwise we probe a small set of common Kiro export prefixes, then fall
# back to enumerating top-level prefixes (one non-recursive call) and
# probing each.
echo
echo "[5/6] Kiro export layout"
probe_user_report() {
    local p="$1"
    aws s3 ls "s3://${KIRO_LOGS_BUCKET}/${p}AWSLogs/${ACCOUNT_ID}/KiroLogs/user_report/" \
        --region "${REGION}" >/dev/null 2>&1
}
if [[ -n "${KIRO_LOGS_BUCKET}" && -n "${ACCOUNT_ID}" ]]; then
    # FOUND tracks whether a probe succeeded; DETECTED_PREFIX holds the value
    # (which may legitimately be the empty string if Kiro writes to the
    # bucket root).
    FOUND=""
    DETECTED_PREFIX=""
    if [[ "${KIRO_LOGS_PREFIX-unset}" != "unset" ]]; then
        # Customer set KIRO_LOGS_PREFIX explicitly (including to empty).
        # Trust it and probe just that location.
        if probe_user_report "${KIRO_LOGS_PREFIX}"; then
            FOUND=1
            DETECTED_PREFIX="${KIRO_LOGS_PREFIX}"
        fi
    else
        for candidate in "" "usage-activity/" "user-activity/" "kiro-logs/" "kiro/"; do
            if probe_user_report "${candidate}"; then
                FOUND=1
                DETECTED_PREFIX="${candidate}"
                break
            fi
        done
        if [[ -z "${FOUND}" ]]; then
            while IFS= read -r p; do
                if probe_user_report "${p}"; then
                    FOUND=1
                    DETECTED_PREFIX="${p}"
                    break
                fi
            done < <(aws s3 ls "s3://${KIRO_LOGS_BUCKET}/" --region "${REGION}" 2>/dev/null \
                     | awk '/^[ ]+PRE / {print $2}')
        fi
    fi

    if [[ -z "${FOUND}" ]]; then
        fail "user_report not found in s3://${KIRO_LOGS_BUCKET}/. Confirm the User Activity Report export is enabled, that it is pointed at this bucket, and that KIRO_LOGS_PREFIX (if set) matches the path above AWSLogs/."
    else
        ok "user_report found at s3://${KIRO_LOGS_BUCKET}/${DETECTED_PREFIX}AWSLogs/${ACCOUNT_ID}/KiroLogs/user_report/"
        if [[ "${KIRO_LOGS_PREFIX-unset}" == "unset" ]]; then
            ok "Detected KIRO_LOGS_PREFIX='${DETECTED_PREFIX}' - export it before running deploy.sh to skip auto-detection"
        fi
        export DETECTED_PREFIX
    fi
fi

# 6) QuickSight S3 + Athena access
# Earlier versions of this script grepped IAM for AWSQuickSightS3Policy. That
# only works on the legacy QS-managed-IAM-role model - modern QS deployments
# use a customer-owned service role and the named policy doesn't exist. We
# instead rely on the deploy step to surface auth failures (the Athena
# DataSource creation does a connection test that returns a precise error
# when QS lacks bucket/Athena access) and only print actionable guidance here.
echo
echo "[6/6] QuickSight S3 access"
if [[ -n "${ACCOUNT_ID}" ]]; then
    cat <<EOF
  [INFO] QuickSight S3 access is granted at deploy time by attaching an
         inline IAM policy ('KiroAnalyticsQuickSightS3Access') to the QS
         service role. It does NOT need to be pre-configured in the QS
         console. The deploy script will prompt before writing the policy.
         Reference: https://repost.aws/knowledge-center/quicksight-permission-errors
EOF
    PASS=$((PASS+1))
fi

echo
echo "=== Summary: ${PASS} OK, ${WARN} warnings, ${FAIL} failures ==="
[[ "${FAIL}" -eq 0 ]] || { echo "Fix the [FAIL] items above before running deploy.sh"; exit 1; }
[[ "${WARN}" -eq 0 ]] || echo "Note: warnings are non-blocking but worth reading."
echo "Ready to deploy."
