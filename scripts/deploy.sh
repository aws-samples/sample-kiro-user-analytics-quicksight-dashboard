#!/usr/bin/env bash
# Deploy the Kiro user analytics dashboard end-to-end.
#
# Required env vars:
#   KIRO_LOGS_BUCKET  - bucket where Kiro writes the User Activity Report
#   QS_PRINCIPAL_ARN  - QuickSight user/group ARN that should own the dashboard
#
# Optional:
#   AWS_REGION         - default us-east-1
#   STACK_PREFIX       - default kiro-analytics
#   HASH_EMAILS        - default false
#   KMS_KEY_ARN        - default ""
#   KIRO_LOGS_PREFIX   - default "" (auto-detected if unset)
#   QS_IAM_ROLE_NAME   - default aws-quicksight-service-role-v0 (the QS-managed
#                        default). Override if your account is configured to
#                        use an existing role under QuickSight -> Manage
#                        account -> Permissions -> IAM role.
#   THEME_MODE         - default light. Set to dark for a dark-mode dashboard.
#   AUTO_APPROVE_IAM   - default false. Set to true to apply the QS S3 inline
#                        policy without prompting (CI / scripted deploys).
#
# Optional - IAM Identity Center user mapping (resolve the report's opaque
# user_id GUIDs to human names). Entirely opt-in; nothing below is provisioned
# unless you enable it. See README "Identity mapping" and SECURITY.md.
#   IDENTITY_MAPPING   - default false. Set true to enable (or you'll be
#                        prompted y/N on an interactive deploy).
#   IDENTITY_STORE_ID  - the Identity Store id (starts with d-). NOT the
#                        instance ARN / ssoins- id. On the IdC Settings page,
#                        or: aws sso-admin list-instances \
#                            --query 'Instances[].IdentityStoreId'
#   IDC_REGION         - region IAM Identity Center lives in (may differ from
#                        AWS_REGION; e.g. eu-west-1 while data is us-east-1).
#   IDC_ROLE_ARN       - optional. Role to assume when IdC is in another
#                        account (org management / delegated admin).
#   IDENTITY_MAP_REFRESH_SCHEDULE - optional cron() for the daily refresh.
#                        Default "cron(30 3 * * ? *)" (03:30 UTC).

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_PREFIX="${STACK_PREFIX:-kiro-analytics}"
HASH_EMAILS="${HASH_EMAILS:-false}"
KMS_KEY_ARN="${KMS_KEY_ARN:-}"
QS_IAM_ROLE_NAME="${QS_IAM_ROLE_NAME:-aws-quicksight-service-role-v0}"
THEME_MODE="${THEME_MODE:-light}"
IDENTITY_MAPPING="${IDENTITY_MAPPING:-false}"
IDENTITY_STORE_ID="${IDENTITY_STORE_ID:-}"
IDC_REGION="${IDC_REGION:-}"
IDC_ROLE_ARN="${IDC_ROLE_ARN:-}"
IDENTITY_MAP_REFRESH_SCHEDULE="${IDENTITY_MAP_REFRESH_SCHEDULE:-cron(30 3 * * ? *)}"

# AWS Glue DB and Athena workgroup names are namespaced to STACK_PREFIX so multiple
# parallel deployments in the same account/region don't collide. AWS Glue DB names
# must be lower-case and use underscores, so swap hyphens.
DATABASE_NAME="${DATABASE_NAME:-${STACK_PREFIX//-/_}}"
WORKGROUP_NAME="${WORKGROUP_NAME:-${STACK_PREFIX//-/_}}"

: "${KIRO_LOGS_BUCKET:?Set KIRO_LOGS_BUCKET to the bucket Kiro exports to}"
: "${QS_PRINCIPAL_ARN:?Set QS_PRINCIPAL_ARN to the QuickSight user/group ARN that should own the dashboard}"

# Accept name, ARN (arn:aws:s3:::name), or s3:// URI for KIRO_LOGS_BUCKET. CFN
# parameters and the IAM policy renderer below need the bare bucket name.
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET#arn:aws:s3:::}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET#s3://}"
KIRO_LOGS_BUCKET="${KIRO_LOGS_BUCKET%/}"

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DATA_STACK="${STACK_PREFIX}-data"
QS_STACK="${STACK_PREFIX}-qs"
IDMAP_STACK="${STACK_PREFIX}-identity-map"

# --- Identity mapping: opt-in gate, prompts, and validation -----------------
# Resolve the opt-in. On an interactive deploy with nothing preset, ask once.
if [[ "${IDENTITY_MAPPING}" != "true" && -t 0 && -z "${IDENTITY_STORE_ID}" ]]; then
    echo
    echo ">> Optional: map the report's opaque user_id GUIDs to human names via"
    echo "   IAM Identity Center. Provisions a small Lambda + a dedicated"
    echo "   KMS-encrypted bucket and pulls active users' names/emails into the"
    echo "   dashboard. Skip to keep today's behavior (user_id shown as-is)."
    read -r -p ">> Enable IAM Identity Center user mapping? [y/N] " reply
    [[ "${reply}" =~ ^[Yy]$ ]] && IDENTITY_MAPPING="true"
fi

if [[ "${IDENTITY_MAPPING}" == "true" ]]; then
    # Mutually exclusive with email hashing: resolving users to real names while
    # hashing their email is contradictory, and writing plaintext display_name
    # next to a hashed email would silently defeat the privacy control.
    if [[ "${HASH_EMAILS}" == "true" ]]; then
        echo ">> ERROR: IDENTITY_MAPPING and HASH_EMAILS are mutually exclusive." >&2
        echo "   Identity mapping resolves users to real names; hashing emails" >&2
        echo "   is a conflicting privacy posture. Enable one, not both." >&2
        exit 1
    fi

    # Prompt for any missing required inputs on an interactive deploy.
    if [[ -z "${IDENTITY_STORE_ID}" && -t 0 ]]; then
        read -r -p ">> Identity Store ID (starts with d-): " IDENTITY_STORE_ID
        IDENTITY_STORE_ID="$(echo "${IDENTITY_STORE_ID}" | tr -d '[:space:]')"
    fi
    if [[ -z "${IDC_REGION}" && -t 0 ]]; then
        read -r -p ">> Region IAM Identity Center lives in [${REGION}]: " IDC_REGION
        IDC_REGION="$(echo "${IDC_REGION}" | tr -d '[:space:]')"
        IDC_REGION="${IDC_REGION:-${REGION}}"
    fi
    IDC_REGION="${IDC_REGION:-${REGION}}"

    : "${IDENTITY_STORE_ID:?Set IDENTITY_STORE_ID (the d-... Identity Store id) to enable identity mapping}"

    # Validate the Identity Store id. Loose check (must start with d-) so an
    # AWS format change never over-rejects; we only hard-fail the common
    # mistake of pasting the instance ARN / ssoins- instance id, which the
    # identitystore API does NOT accept.
    if [[ "${IDENTITY_STORE_ID}" != d-* ]]; then
        echo ">> ERROR: IDENTITY_STORE_ID must be an Identity Store ID (starts with 'd-')." >&2
        if [[ "${IDENTITY_STORE_ID}" == arn:* || "${IDENTITY_STORE_ID}" == ssoins-* ]]; then
            echo "   That looks like the Identity Center *instance* id/ARN. The" >&2
            echo "   identitystore API needs the Identity *Store* id instead." >&2
        fi
        echo "   Find it on the IdC Settings page, or run:" >&2
        echo "     aws sso-admin list-instances --query 'Instances[].IdentityStoreId'" >&2
        exit 1
    fi

    echo ">> Identity mapping ENABLED: store=${IDENTITY_STORE_ID} idc-region=${IDC_REGION}${IDC_ROLE_ARN:+ role=${IDC_ROLE_ARN}}"
fi

# Auto-detect KIRO_LOGS_PREFIX if not set. Setting it explicitly is strongly
# recommended (see README); auto-detect is a courtesy fallback. We avoid
# `aws s3 ls --recursive` because on large buckets that hold other content
# it paginates through every key. Instead:
#   1. Probe a small set of common Kiro export prefixes by checking whether
#      `<prefix>AWSLogs/<acct>/KiroLogs/user_report/` exists (one cheap
#      HEAD-equivalent call per probe).
#   2. If none match, list only the top-level prefixes of the bucket (one
#      non-recursive call, which returns one row per "directory" regardless
#      of how many objects sit under it) and probe each.
KIRO_LOGS_PREFIX_FOUND=""
probe_prefix() {
    local p="$1"
    aws s3 ls "s3://${KIRO_LOGS_BUCKET}/${p}AWSLogs/${ACCOUNT_ID}/KiroLogs/user_report/" \
        --region "${REGION}" >/dev/null 2>&1
}
if [[ "${KIRO_LOGS_PREFIX-unset}" == "unset" ]]; then
    # KIRO_LOGS_PREFIX is not set at all (vs explicitly set to empty by the
    # caller, in which case we trust the caller).
    for candidate in "" "usage-activity/" "user-activity/" "kiro-logs/" "kiro/"; do
        if probe_prefix "${candidate}"; then
            KIRO_LOGS_PREFIX="${candidate}"
            KIRO_LOGS_PREFIX_FOUND=1
            break
        fi
    done
    if [[ -z "${KIRO_LOGS_PREFIX_FOUND}" ]]; then
        # Fall back to enumerating top-level prefixes and probing each.
        while IFS= read -r p; do
            if probe_prefix "${p}"; then
                KIRO_LOGS_PREFIX="${p}"
                KIRO_LOGS_PREFIX_FOUND=1
                break
            fi
        done < <(aws s3 ls "s3://${KIRO_LOGS_BUCKET}/" --region "${REGION}" 2>/dev/null \
                 | awk '/^[ ]+PRE / {print $2}')
    fi
    if [[ -n "${KIRO_LOGS_PREFIX_FOUND}" ]]; then
        echo ">> Auto-detected KIRO_LOGS_PREFIX='${KIRO_LOGS_PREFIX}'"
    else
        KIRO_LOGS_PREFIX=""
        echo ">> Could not detect prefix; assuming bucket root. Run scripts/preflight.sh if this looks wrong."
    fi
fi

echo ">> Account=${ACCOUNT_ID} Region=${REGION} Bucket=${KIRO_LOGS_BUCKET} Prefix='${KIRO_LOGS_PREFIX}'"

# 1) Data layer (AWS Glue + Athena + report-normalizer Lambda).
# The normalizer Lambda's code zip must live in the results bucket before the
# stack can create the function, but the results bucket is created BY this
# stack - so on a first-ever deploy we deploy once with an empty code key
# (creates bucket + role, no function), then stage the zip and redeploy with
# the real key (adds the function). On re-deploys the bucket already exists, so
# we stage up-front and deploy once.
RESULTS_BUCKET="${DATA_STACK}-athena-results-${ACCOUNT_ID}-${REGION}"
echo ">> Packaging report-normalizer Lambda"
NRM_BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${NRM_BUILD_DIR}"' EXIT
cp "${ROOT}/scripts/normalize_report_lambda.py" "${NRM_BUILD_DIR}/"
( cd "${NRM_BUILD_DIR}" && zip -q normalize_report_lambda.zip normalize_report_lambda.py )
# Key by content hash so unchanged code keeps the same key (no needless
# function update on re-deploy); changed code gets a new key.
if command -v sha256sum >/dev/null 2>&1; then
    NRM_SHA="$(sha256sum "${NRM_BUILD_DIR}/normalize_report_lambda.py" | awk '{print $1}')"
else
    NRM_SHA="$(shasum -a 256 "${NRM_BUILD_DIR}/normalize_report_lambda.py" | awk '{print $1}')"
fi
NRM_KEY="lambda-code/normalize_report_lambda-${NRM_SHA}.zip"
NRM_CODE_KEY=""
if aws s3 ls "s3://${RESULTS_BUCKET}/" --region "${REGION}" >/dev/null 2>&1; then
    aws s3 cp "${NRM_BUILD_DIR}/normalize_report_lambda.zip" \
        "s3://${RESULTS_BUCKET}/${NRM_KEY}" --region "${REGION}"
    NRM_CODE_KEY="${NRM_KEY}"
fi

echo ">> Deploying ${DATA_STACK}"
aws cloudformation deploy \
    --region "${REGION}" \
    --stack-name "${DATA_STACK}" \
    --template-file "${ROOT}/cfn/01-data-layer.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        "KiroLogsBucketName=${KIRO_LOGS_BUCKET}" \
        "KiroLogsPrefix=${KIRO_LOGS_PREFIX}" \
        "KmsKeyArn=${KMS_KEY_ARN}" \
        "DatabaseName=${DATABASE_NAME}" \
        "WorkgroupName=${WORKGROUP_NAME}" \
        "NormalizerLambdaCodeS3Key=${NRM_CODE_KEY}"

if [[ -z "${NRM_CODE_KEY}" ]]; then
    echo ">> Staging normalizer zip (first deploy) and re-deploying ${DATA_STACK}"
    aws s3 cp "${NRM_BUILD_DIR}/normalize_report_lambda.zip" \
        "s3://${RESULTS_BUCKET}/${NRM_KEY}" --region "${REGION}"
    aws cloudformation deploy \
        --region "${REGION}" \
        --stack-name "${DATA_STACK}" \
        --template-file "${ROOT}/cfn/01-data-layer.yaml" \
        --capabilities CAPABILITY_IAM \
        --parameter-overrides \
            "KiroLogsBucketName=${KIRO_LOGS_BUCKET}" \
            "KiroLogsPrefix=${KIRO_LOGS_PREFIX}" \
            "KmsKeyArn=${KMS_KEY_ARN}" \
            "DatabaseName=${DATABASE_NAME}" \
            "WorkgroupName=${WORKGROUP_NAME}" \
            "NormalizerLambdaCodeS3Key=${NRM_KEY}"
fi

DATABASE="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${DATA_STACK}" \
    --query "Stacks[0].Outputs[?OutputKey=='GlueDatabaseName'].OutputValue" --output text)"
WORKGROUP="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${DATA_STACK}" \
    --query "Stacks[0].Outputs[?OutputKey=='AthenaWorkgroupName'].OutputValue" --output text)"
NORMALIZER_LAMBDA="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${DATA_STACK}" \
    --query "Stacks[0].Outputs[?OutputKey=='NormalizerLambdaName'].OutputValue" --output text 2>/dev/null || echo "")"

# 1b) Guard against a reused-prefix / changed-bucket mismatch.
# Re-running an existing STACK_PREFIX with a different KIRO_LOGS_BUCKET reports
# "No changes" on the data stack, so the normalizer Lambda keeps its ORIGINAL
# RAW_BUCKET env while the IAM grants / passed bucket point elsewhere - the
# dashboard would then show stale data from the old bucket. We compare the
# deployed normalizer's RAW_BUCKET (what actually gets read) against the bucket
# passed now. Skipped cleanly on a first deploy (function doesn't exist yet).
deployed_raw="$(aws lambda get-function-configuration --region "${REGION}" \
    --function-name "${DATA_STACK}-normalize-report" \
    --query 'Environment.Variables.RAW_BUCKET' --output text 2>/dev/null || echo "")"
if [[ -n "${deployed_raw}" && "${deployed_raw}" != "None" && "${deployed_raw}" != "${KIRO_LOGS_BUCKET}" ]]; then
    echo ">> ERROR: stack '${DATA_STACK}' is already wired to a different bucket." >&2
    echo "     normalizer reads RAW_BUCKET: ${deployed_raw}" >&2
    echo "     you passed KIRO_LOGS_BUCKET:  ${KIRO_LOGS_BUCKET}" >&2
    echo "   Re-running an existing prefix with a different bucket reports" >&2
    echo "   'No changes' on the stack, so reads stay on the original bucket." >&2
    echo "   To proceed, either:" >&2
    echo "     - use the original bucket (KIRO_LOGS_BUCKET=${deployed_raw}), or" >&2
    echo "     - pick a new STACK_PREFIX for a fresh deployment, or" >&2
    echo "     - tear down (scripts/teardown.sh) and redeploy against the new bucket." >&2
    exit 1
fi

# 2a) Normalize the raw report. Reads the raw Kiro CSVs from S3 BY HEADER and
# writes the fixed-schema normalized/facts + normalized/models objects. Runs
# BEFORE build_views.py so those objects exist when build_views creates
# the report_facts / report_models external tables). Synchronous so the first
# dashboard open reads correct data; the stack's daily schedule keeps it fresh.
# Best-effort: a failure here must not brick the deploy - build_views still
# creates the (empty) external tables and the daily schedule retries.
if [[ -n "${NORMALIZER_LAMBDA}" && "${NORMALIZER_LAMBDA}" != "None" ]]; then
    echo ">> Normalizing raw report (synchronous Lambda invoke)"
    set +e
    nrm_err="$(aws lambda invoke --region "${REGION}" \
        --function-name "${NORMALIZER_LAMBDA}" \
        --cli-binary-format raw-in-base64-out --payload '{}' \
        --query 'FunctionError' --output text \
        "${NRM_BUILD_DIR}/invoke.json" 2>"${NRM_BUILD_DIR}/invoke.stderr")"
    nrm_rc=$?
    set -e
    if [[ ${nrm_rc} -ne 0 || ( -n "${nrm_err}" && "${nrm_err}" != "None" ) ]]; then
        echo ">> WARNING: normalizer Lambda invoke failed:" >&2
        cat "${NRM_BUILD_DIR}/invoke.stderr" "${NRM_BUILD_DIR}/invoke.json" 2>/dev/null >&2
        echo "   Views will be created over (possibly empty) normalized tables;" >&2
        echo "   the daily schedule will retry. Check the Lambda logs." >&2
    else
        echo "   Normalizer result: $(cat "${NRM_BUILD_DIR}/invoke.json")"
    fi
fi

# 2a-ii) Create the report_facts / report_models external tables now (before
# identity mapping), so the identity-map Lambda's SELECT DISTINCT userid FROM
# report_facts works. The full view build runs again in step 3.
echo ">> Creating report_facts / report_models external tables"
python3 "${ROOT}/scripts/build_views.py" \
    --database "${DATABASE}" \
    --workgroup "${WORKGROUP}" \
    --region "${REGION}" \
    --normalized-bucket "${RESULTS_BUCKET}" \
    --tables-only

# 2b) Identity mapping (optional). Must run AFTER the report tables exist (so
# the Lambda's distinct-user query against report_facts works) and BEFORE
# build_views.py (so the identity_map CSV exists when build_views creates the
# external table + join). This deploy-time synchronous population is why real
# names show up on the FIRST dashboard open; the stack's daily schedule only
# keeps the map fresh thereafter.
IDMAP_BUCKET=""
IDMAP_KEY_ARN=""
PURGE_IDMAP=""
if [[ "${IDENTITY_MAPPING}" == "true" ]]; then
    echo ">> Packaging identity-map Lambda"
    BUILD_DIR="$(mktemp -d)"
    trap 'rm -rf "${BUILD_DIR}"' EXIT
    cp "${ROOT}/scripts/identity_map_lambda.py" "${BUILD_DIR}/"
    ( cd "${BUILD_DIR}" && zip -q identity_map_lambda.zip identity_map_lambda.py )
    # Key by content hash: unchanged code keeps the same key (no needless
    # Lambda update on re-deploy); changed code gets a new key so CFN updates
    # the function. Use sha256sum (Linux / CloudShell) or shasum -a 256
    # (macOS) - whichever is present produces the same hex digest.
    if command -v sha256sum >/dev/null 2>&1; then
        LAMBDA_SHA="$(sha256sum "${BUILD_DIR}/identity_map_lambda.py" | awk '{print $1}')"
    else
        LAMBDA_SHA="$(shasum -a 256 "${BUILD_DIR}/identity_map_lambda.py" | awk '{print $1}')"
    fi
    LAMBDA_KEY="lambda-code/identity_map_lambda-${LAMBDA_SHA}.zip"
    aws s3 cp "${BUILD_DIR}/identity_map_lambda.zip" \
        "s3://${RESULTS_BUCKET}/${LAMBDA_KEY}" --region "${REGION}"

    echo ">> Deploying ${IDMAP_STACK}"
    aws cloudformation deploy \
        --region "${REGION}" \
        --stack-name "${IDMAP_STACK}" \
        --template-file "${ROOT}/cfn/03-identity-mapping.yaml" \
        --capabilities CAPABILITY_IAM \
        --parameter-overrides \
            "ResourcePrefix=${STACK_PREFIX}" \
            "IdentityStoreId=${IDENTITY_STORE_ID}" \
            "IdcRegion=${IDC_REGION}" \
            "IdcRoleArn=${IDC_ROLE_ARN}" \
            "SourceKmsKeyArn=${KMS_KEY_ARN}" \
            "AthenaDatabase=${DATABASE}" \
            "AthenaWorkgroup=${WORKGROUP}" \
            "AthenaResultsBucket=${RESULTS_BUCKET}" \
            "KiroLogsBucket=${KIRO_LOGS_BUCKET}" \
            "LambdaCodeS3Key=${LAMBDA_KEY}" \
            "RefreshSchedule=${IDENTITY_MAP_REFRESH_SCHEDULE}"

    IDMAP_BUCKET="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${IDMAP_STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='IdentityMapBucketName'].OutputValue" --output text)"
    IDMAP_LAMBDA="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${IDMAP_STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='IdentityMapLambdaName'].OutputValue" --output text)"
    IDMAP_KEY_ARN="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${IDMAP_STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='IdentityMapKeyArn'].OutputValue" --output text)"

    # Seed a header-only CSV if none exists yet, so the external table + LEFT
    # JOIN never error on a brand-new deployment where the Lambda finds no
    # users (e.g. zero activity exports). The Lambda overwrites this with real
    # data whenever it resolves active users.
    if ! aws s3 ls "s3://${IDMAP_BUCKET}/identity-map/users.csv" --region "${REGION}" >/dev/null 2>&1; then
        printf '"user_id","username","display_name","email"\n' > "${BUILD_DIR}/seed.csv"
        aws s3 cp "${BUILD_DIR}/seed.csv" \
            "s3://${IDMAP_BUCKET}/identity-map/users.csv" --region "${REGION}"
        echo "   seeded empty identity map (safety net)"
    fi

    # Populate synchronously so names are present on first dashboard open.
    # Best-effort: an IdC access failure must NOT brick the dashboard deploy -
    # the dashboard simply falls back to user_id and the daily schedule retries.
    echo ">> Populating identity map (synchronous Lambda invoke)"
    set +e
    invoke_err="$(aws lambda invoke --region "${REGION}" \
        --function-name "${IDMAP_LAMBDA}" \
        --cli-binary-format raw-in-base64-out --payload '{}' \
        --query 'FunctionError' --output text \
        "${BUILD_DIR}/invoke.json" 2>"${BUILD_DIR}/invoke.stderr")"
    invoke_rc=$?
    set -e
    if [[ ${invoke_rc} -ne 0 ]]; then
        echo ">> WARNING: could not invoke identity-map Lambda:" >&2
        cat "${BUILD_DIR}/invoke.stderr" >&2
        echo "   Dashboard will fall back to user_id; the daily schedule will retry." >&2
    elif [[ -n "${invoke_err}" && "${invoke_err}" != "None" ]]; then
        echo ">> WARNING: identity-map Lambda reported ${invoke_err}:" >&2
        cat "${BUILD_DIR}/invoke.json" >&2; echo >&2
        echo "   Dashboard will fall back to user_id; the daily schedule will retry." >&2
    else
        echo "   Lambda result: $(cat "${BUILD_DIR}/invoke.json")"
    fi
else
    # Mapping is OFF. If a previous deploy turned it ON, stack 03 (Lambda +
    # PII bucket) still exists - flag it for teardown after we rebuild the
    # views without the identity join and purge names from SPICE.
    if aws cloudformation describe-stacks --region "${REGION}" \
            --stack-name "${IDMAP_STACK}" >/dev/null 2>&1; then
        PURGE_IDMAP=1
        echo ">> Identity mapping is OFF but ${IDMAP_STACK} exists - will remove"
        echo "   it and purge resolved names from SPICE after rebuilding views."
    fi
fi

# 3) Curated Athena views.
echo ">> Building curated Athena views"
VIEW_FLAGS=()
[[ "${HASH_EMAILS}" == "true" ]] && VIEW_FLAGS+=(--hash-emails)
[[ -n "${IDMAP_BUCKET}" ]] && VIEW_FLAGS+=(--identity-map-bucket "${IDMAP_BUCKET}")
python3 "${ROOT}/scripts/build_views.py" \
    --database "${DATABASE}" \
    --workgroup "${WORKGROUP}" \
    --region "${REGION}" \
    --normalized-bucket "${RESULTS_BUCKET}" \
    "${VIEW_FLAGS[@]+"${VIEW_FLAGS[@]}"}"

# 4) QuickSight S3 access. The QS service role needs ListBucket/GetObject on
# the Kiro logs bucket and the Athena results bucket. We check the IAM
# policy QS uses behind the scenes; if buckets are missing, prompt the user
# to apply automatically or stop here so they can do it manually.
# (RESULTS_BUCKET is set above, before the identity-map block.)
echo ">> Checking QuickSight S3 access"
echo ">> Will modify IAM role: ${QS_IAM_ROLE_NAME}"
echo "   (the QS-managed default. If your QS account is configured under"
echo "    QuickSight -> Manage account -> Permissions -> IAM role -> 'Use an"
echo "    existing role', answer 'n' below to enter your role name.)"
if [[ -t 0 ]]; then
    read -r -p ">> Proceed with role '${QS_IAM_ROLE_NAME}'? [Y/n] " reply
    if [[ "${reply}" =~ ^[Nn]$ ]]; then
        read -r -p ">> Enter the IAM role name QuickSight uses: " custom_role
        custom_role="$(echo "${custom_role}" | tr -d '[:space:]')"
        if [[ -z "${custom_role}" ]]; then
            echo ">> No role provided. Stopping." >&2
            exit 1
        fi
        QS_IAM_ROLE_NAME="${custom_role}"
        echo ">> Using IAM role: ${QS_IAM_ROLE_NAME}"
    fi
fi
# Kiro logs bucket is read-only (source data). Athena results bucket needs
# write too - Athena queries run by QuickSight write their results there.
QS_S3_BUCKET_SPECS=("${KIRO_LOGS_BUCKET}:read" "${RESULTS_BUCKET}:read_write")
# When identity mapping is on, QS/Athena also read the SSE-KMS identity-map
# bucket; grant read on the bucket + kms:Decrypt on its CMK.
QS_KMS_ARGS=()
if [[ -n "${IDMAP_BUCKET}" ]]; then
    QS_S3_BUCKET_SPECS+=("${IDMAP_BUCKET}:read")
    QS_KMS_ARGS=(--kms-key-arn "${IDMAP_KEY_ARN}")
fi
# Guarded expansion: macOS bash 3.2 trips `set -u` on an empty array.
set +e
python3 "${ROOT}/scripts/grant_quicksight_s3.py" \
    --region "${REGION}" \
    --role-name "${QS_IAM_ROLE_NAME}" \
    --buckets "${QS_S3_BUCKET_SPECS[@]}" \
    "${QS_KMS_ARGS[@]+"${QS_KMS_ARGS[@]}"}"
QS_S3_RC=$?
set -e
case "${QS_S3_RC}" in
    0)
        ;;  # already authorized
    1)
        # Missing buckets / actions - instructions already printed.
        # AUTO_APPROVE_IAM=true skips the prompt and applies in one shot
        # (intended for CI / scripted deploys).
        if [[ "${AUTO_APPROVE_IAM:-false}" == "true" ]]; then
            echo ">> AUTO_APPROVE_IAM=true; applying without prompt"
            python3 "${ROOT}/scripts/grant_quicksight_s3.py" --apply \
                --region "${REGION}" \
                --role-name "${QS_IAM_ROLE_NAME}" \
                --buckets "${QS_S3_BUCKET_SPECS[@]}" \
                "${QS_KMS_ARGS[@]+"${QS_KMS_ARGS[@]}"}"
        elif [[ -t 0 ]]; then
            read -r -p ">> Apply the IAM update now? [y/N] " reply
            if [[ "${reply}" =~ ^[Yy]$ ]]; then
                python3 "${ROOT}/scripts/grant_quicksight_s3.py" --apply \
                    --region "${REGION}" \
                    --role-name "${QS_IAM_ROLE_NAME}" \
                    --buckets "${QS_S3_BUCKET_SPECS[@]}" \
                    "${QS_KMS_ARGS[@]+"${QS_KMS_ARGS[@]}"}"
            else
                echo ">> Stopping. Update QS S3 access via the steps above and re-run deploy.sh." >&2
                exit 1
            fi
        else
            echo ">> Non-interactive shell. Either:" >&2
            echo "     - re-run with AUTO_APPROVE_IAM=true to apply automatically, or" >&2
            echo "     - run the --apply command shown above, then re-run deploy.sh." >&2
            exit 1
        fi
        ;;
    *)
        exit "${QS_S3_RC}"
        ;;
esac

# 4b) Verify AWSQuicksightAthenaAccess managed policy is attached to the QS
# service role. QuickSight is supposed to attach this when the customer
# checks "Athena" under Manage account -> Permissions -> AWS resources and
# clicks Save. We have seen accounts where the save was a no-op (Athena was
# already checked, no real change) and the policy never got attached - the
# Athena DataSource then fails to create with "QuickSight service role
# required to access your AWS resources has not been created yet".
# The canonical name in IAM is AWSQuicksightAthenaAccess (lowercase 's' in
# Quicksight). IAM treats the ARN as case-insensitive for resolution, so the
# CamelCase variant also works at attach time, but we match case-insensitively
# here so the check does not falsely trigger a re-attach when the role
# already has the policy under its canonical lowercase name.
ATHENA_POLICY_ARN="arn:aws:iam::aws:policy/service-role/AWSQuicksightAthenaAccess"
echo ">> Checking AWSQuicksightAthenaAccess on ${QS_IAM_ROLE_NAME}"
if aws iam list-attached-role-policies --role-name "${QS_IAM_ROLE_NAME}" \
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null \
        | tr '\t' '\n' | grep -Fixq "${ATHENA_POLICY_ARN}"; then
    echo "   already attached"
else
    echo ">> AWSQuicksightAthenaAccess is NOT attached to ${QS_IAM_ROLE_NAME}."
    echo "   QuickSight requires this managed policy to query Athena. Without"
    echo "   it the AthenaDataSource will fail to create."
    if [[ "${AUTO_APPROVE_IAM:-false}" == "true" ]]; then
        echo ">> AUTO_APPROVE_IAM=true; attaching without prompt"
        aws iam attach-role-policy \
            --role-name "${QS_IAM_ROLE_NAME}" \
            --policy-arn "${ATHENA_POLICY_ARN}"
        echo "   attached"
    elif [[ -t 0 ]]; then
        read -r -p ">> Attach AWSQuicksightAthenaAccess now? [y/N] " reply
        if [[ "${reply}" =~ ^[Yy]$ ]]; then
            aws iam attach-role-policy \
                --role-name "${QS_IAM_ROLE_NAME}" \
                --policy-arn "${ATHENA_POLICY_ARN}"
            echo "   attached"
        else
            echo ">> Stopping. Attach the policy with:" >&2
            echo "     aws iam attach-role-policy --role-name ${QS_IAM_ROLE_NAME} \\" >&2
            echo "         --policy-arn ${ATHENA_POLICY_ARN}" >&2
            exit 1
        fi
    else
        echo ">> Non-interactive shell. Either:" >&2
        echo "     - re-run with AUTO_APPROVE_IAM=true to attach automatically, or" >&2
        echo "     - run: aws iam attach-role-policy --role-name ${QS_IAM_ROLE_NAME} \\" >&2
        echo "                --policy-arn ${ATHENA_POLICY_ARN}" >&2
        echo "       then re-run deploy.sh." >&2
        exit 1
    fi
fi

# 5) QuickSight DataSource + DataSets.
# CloudFormation refuses to update a stack that's stuck in ROLLBACK_COMPLETE
# (the state a stack lands in when its first create fails). Detect it and
# delete the husk so the deploy can proceed.
qs_status="$(aws cloudformation describe-stacks --stack-name "${QS_STACK}" \
    --region "${REGION}" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "")"
if [[ "${qs_status}" == "ROLLBACK_COMPLETE" ]]; then
    echo ">> ${QS_STACK} is in ROLLBACK_COMPLETE - deleting before re-deploy"
    aws cloudformation delete-stack --stack-name "${QS_STACK}" --region "${REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${QS_STACK}" --region "${REGION}"
fi

echo ">> Deploying ${QS_STACK} (DataSource + DataSets)"
aws cloudformation deploy \
    --region "${REGION}" \
    --stack-name "${QS_STACK}" \
    --template-file "${ROOT}/cfn/02-quicksight.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        "GlueDatabase=${DATABASE}" \
        "AthenaWorkgroup=${WORKGROUP}" \
        "QuickSightPrincipalArn=${QS_PRINCIPAL_ARN}" \
        "ResourcePrefix=${STACK_PREFIX}" \
        "ThemeMode=${THEME_MODE}"

# 6) Analysis + Dashboard via boto3 - see scripts/create_dashboard.py.
# Asset ID matches STACK_PREFIX so parallel deployments don't overwrite each
# other's analysis/dashboard.
ASSET_ID="${STACK_PREFIX}"
echo ">> Creating QuickSight Analysis + Dashboard"
THEME_ARN="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${QS_STACK}" \
    --query "Stacks[0].Outputs[?OutputKey=='ThemeArn'].OutputValue" --output text 2>/dev/null || echo "")"
python3 "${ROOT}/scripts/create_dashboard.py" \
    --account-id "${ACCOUNT_ID}" \
    --region "${REGION}" \
    --principal-arn "${QS_PRINCIPAL_ARN}" \
    --asset-id "${ASSET_ID}" \
    --resource-prefix "${STACK_PREFIX}" \
    ${THEME_ARN:+--theme-arn "${THEME_ARN}"}

# 6b) When identity mapping is ON, force a SPICE refresh of the datasets whose
# views resolve user_label. The QS datasets are SPICE import mode, so a view
# that now returns names does NOT change the dashboard until the data is
# re-ingested. On a brand-new deploy the dataset CREATE auto-ingests after the
# views are built, so names appear on first open - but enabling mapping on an
# ALREADY-deployed dashboard leaves stack 02 with "no changes", so without this
# the names would not show until the next daily 04:00 refresh. Only the
# datasets whose views carry user_label are refreshed (cheap + sufficient);
# funnel/cohort join user_dim for tier only, so they are unaffected.
if [[ -n "${IDMAP_BUCKET}" ]]; then
    echo ">> Refreshing SPICE for identity-resolved datasets"
    for ds in base-user-activity user-totals engagement wow-movers model-usage; do
        # Best-effort: a busy/refreshing dataset returns an error we ignore -
        # its daily schedule will pick up the new data regardless.
        aws quicksight create-ingestion --region "${REGION}" \
            --aws-account-id "${ACCOUNT_ID}" \
            --data-set-id "${STACK_PREFIX}-${ds}" \
            --ingestion-id "idmap-$(date +%Y%m%d%H%M%S)" \
            --ingestion-type FULL_REFRESH >/dev/null 2>&1 \
            && echo "   re-ingesting ${STACK_PREFIX}-${ds}" \
            || echo "   (skip ${STACK_PREFIX}-${ds}: busy or refreshing; daily schedule will catch up)" >&2
    done
fi

# 7) Opt-out cleanup. If mapping was just turned OFF but stack 03 still exists,
# the views were already rebuilt name-free above; now flush any resolved names
# that linger in SPICE (datasets are SPICE import mode), then remove the PII
# infrastructure entirely.
if [[ -n "${PURGE_IDMAP}" ]]; then
    echo ">> Purging resolved names from SPICE (full refresh of all datasets)"
    DATASETS=(
        base-user-activity daily-trends user-totals tier-breakdown engagement
        model-usage wow-movers period-comparison cohort-retention
        activity-heatmap
    )
    for ds in "${DATASETS[@]}"; do
        # Best-effort: a single dataset failing to ingest must not stop the
        # purge of the rest or the stack teardown.
        aws quicksight create-ingestion --region "${REGION}" \
            --aws-account-id "${ACCOUNT_ID}" \
            --data-set-id "${STACK_PREFIX}-${ds}" \
            --ingestion-id "optout-$(date +%Y%m%d%H%M%S)" \
            --ingestion-type FULL_REFRESH >/dev/null 2>&1 \
            && echo "   re-ingesting ${STACK_PREFIX}-${ds}" \
            || echo "   (skip ${STACK_PREFIX}-${ds}: not found or busy)" >&2
    done

    # Recover the PII bucket name from the stack before deleting it, so we can
    # purge its objects (the bucket has DeletionPolicy: Retain and won't be
    # removed by stack deletion while non-empty).
    OPTOUT_BUCKET="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${IDMAP_STACK}" \
        --query "Stacks[0].Outputs[?OutputKey=='IdentityMapBucketName'].OutputValue" --output text 2>/dev/null || echo "")"

    # Drop the now-orphaned identity_map Glue table (build_views with mapping
    # off does not touch it, and after teardown it points at a deleted bucket).
    echo ">> Dropping the identity_map table"
    python3 - "${DATABASE}" "${WORKGROUP}" "${REGION}" <<'PY' || echo "   (could not drop identity_map; drop it manually)" >&2
import sys, time, boto3
database, workgroup, region = sys.argv[1], sys.argv[2], sys.argv[3]
athena = boto3.client("athena", region_name=region)
qid = athena.start_query_execution(
    QueryString=f"DROP TABLE IF EXISTS {database}.identity_map",
    QueryExecutionContext={"Database": database},
    WorkGroup=workgroup,
)["QueryExecutionId"]
while True:
    st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
    if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
        break
    time.sleep(1)
sys.exit(0 if st == "SUCCEEDED" else 1)
PY

    # Revoke the whole inline QS policy, then re-apply ONLY the base buckets.
    # grant_quicksight_s3.py is additive (policy_covers treats extra resources
    # as already-satisfied), so simply re-applying with fewer buckets would NOT
    # remove the identity-map bucket / KMS statements - we must revoke first.
    echo ">> Removing the QuickSight inline grant for the identity-map bucket/key"
    python3 "${ROOT}/scripts/grant_quicksight_s3.py" --revoke \
        --region "${REGION}" \
        --role-name "${QS_IAM_ROLE_NAME}" \
        --buckets "${KIRO_LOGS_BUCKET}:read" \
        || echo "   (could not revoke QS grant; remove the identity-map statements manually)" >&2
    python3 "${ROOT}/scripts/grant_quicksight_s3.py" --apply \
        --region "${REGION}" \
        --role-name "${QS_IAM_ROLE_NAME}" \
        --buckets "${KIRO_LOGS_BUCKET}:read" "${RESULTS_BUCKET}:read_write" \
        || echo "   (could not re-apply base QS grant; re-run deploy.sh)" >&2

    # Purge AND delete both buckets. They have DeletionPolicy: Retain, so
    # deleting the stack alone would leave them behind - and an empty retained
    # bucket then collides with the stack's CREATE on a later re-enable
    # (ResourceExistenceCheck). Emptying every version + deleting the bucket
    # (same approach as teardown.sh) ensures no resolved names survive and a
    # future re-enable can recreate cleanly.
    OPTOUT_LOG_BUCKET="${STACK_PREFIX}-idmap-logs-${ACCOUNT_ID}-${REGION}"
    for b in "${OPTOUT_BUCKET}" "${OPTOUT_LOG_BUCKET}"; do
        [[ -z "${b}" ]] && continue
        if aws s3api head-bucket --bucket "${b}" --region "${REGION}" >/dev/null 2>&1; then
            echo ">> Purging + deleting bucket ${b}"
            python3 - "${b}" "${REGION}" <<'PY' || echo "   (version purge had issues; check the bucket manually)" >&2
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
                || echo "   ${b}: delete deferred (may be stack-managed)" >&2
        fi
    done

    echo ">> Deleting ${IDMAP_STACK}"
    aws cloudformation delete-stack --region "${REGION}" --stack-name "${IDMAP_STACK}"
    aws cloudformation wait stack-delete-complete --region "${REGION}" --stack-name "${IDMAP_STACK}" \
        || echo "   (stack delete still settling; check for a retained KMS key)" >&2
    echo ">> Identity mapping fully removed."
fi

echo ">> Done. Dashboard: https://${REGION}.quicksight.aws.amazon.com/sn/dashboards/${ASSET_ID}"
