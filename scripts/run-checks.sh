#!/usr/bin/env bash
# Run every offline check for this repo: unit tests plus shell, CloudFormation
# and Python linting. No AWS account, no credentials, no network.
#
# This is what CI runs, and what you should run before opening a PR:
#
#     scripts/run-checks.sh
#
# Exits non-zero if any check fails. Linters that are not installed are SKIPPED
# with a notice rather than failing the run, so a contributor without cfn-lint
# still gets the tests. CI installs them, so a PR is always fully linted.

set -uo pipefail

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "${ROOT}" || exit 1

FAILED=0
SKIPPED=()

section() { printf '\n=== %s ===\n' "$1"; }
pass()    { printf '  [OK]   %s\n' "$1"; }
fail()    { printf '  [FAIL] %s\n' "$1"; FAILED=1; }
skip()    { printf '  [SKIP] %s\n' "$1"; SKIPPED+=("$1"); }

# 1) Unit tests -----------------------------------------------------------------
# The behavioural invariants whose violation is SILENT on the dashboard.
section "Unit tests"
if PYTHONPATH=tests python3 -m unittest discover -s tests -q; then
    pass "unittest"
else
    fail "unittest"
fi

# 2) Python syntax --------------------------------------------------------------
section "Python syntax"
if python3 -m compileall -q scripts/ >/dev/null; then
    pass "compileall scripts/"
else
    fail "compileall scripts/"
fi

# 3) Shell syntax, under BOTH bash versions we support --------------------------
# macOS still ships bash 3.2, and the scripts deliberately avoid bash-4 syntax.
# Parsing under 3.2 is what actually catches a regression there.
section "Shell syntax"
for sh in scripts/*.sh; do
    if bash -n "${sh}" 2>/dev/null; then
        pass "bash -n ${sh##*/}"
    else
        fail "bash -n ${sh##*/}"
    fi
done
if [[ -x /bin/bash ]] && /bin/bash --version | head -1 | grep -q 'version 3'; then
    for sh in scripts/*.sh; do
        if /bin/bash -n "${sh}" 2>/dev/null; then
            pass "bash 3.2 parse ${sh##*/}"
        else
            fail "bash 3.2 parse ${sh##*/}"
        fi
    done
else
    skip "bash 3.2 parse (no bash 3.x at /bin/bash on this machine; CI covers it)"
fi

# 4) Bash-4-only constructs and GNU-only tools ---------------------------------
# The scripts must run on macOS bash 3.2 and BSD userland as well as on Linux.
# This is a grep gate rather than a linter rule because shellcheck does not know
# which bash version we target.
section "Portability"
SCAN_SH=(deploy.sh preflight.sh teardown.sh)
BAD_BASH='mapfile|readarray|declare -A|\$\{[A-Za-z_][A-Za-z0-9_]*,,\}|\$\{[A-Za-z_][A-Za-z0-9_]*\^\^\}|coproc|wait -n'
BAD_GNU='sed -i |date -d |grep -P |readlink -f |stat -c |base64 -w '
SCAN_PATHS=()
for f in "${SCAN_SH[@]}"; do SCAN_PATHS+=("scripts/${f}"); done
if grep -nE "${BAD_BASH}" "${SCAN_PATHS[@]}" >/dev/null 2>&1; then
    grep -nE "${BAD_BASH}" "${SCAN_PATHS[@]}"
    fail "bash 4+ construct (breaks macOS bash 3.2)"
else
    pass "no bash 4+ constructs"
fi
if grep -nE "${BAD_GNU}" "${SCAN_PATHS[@]}" >/dev/null 2>&1; then
    grep -nE "${BAD_GNU}" "${SCAN_PATHS[@]}"
    fail "GNU-only tool usage (breaks BSD/macOS userland)"
else
    pass "no GNU-only tool usage"
fi

# 5) shellcheck ----------------------------------------------------------------
section "shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
    for sh in scripts/*.sh; do
        # SC1091: sourced files not followed. SC2016: single quotes in a
        # deliberately-unexpanded string (we pass literal ${...} to CFN/awk).
        if shellcheck -s bash -e SC1091,SC2016 "${sh}"; then
            pass "shellcheck ${sh##*/}"
        else
            fail "shellcheck ${sh##*/}"
        fi
    done
else
    skip "shellcheck (not installed)"
fi

# 6) cfn-lint ------------------------------------------------------------------
section "cfn-lint"
if command -v cfn-lint >/dev/null 2>&1; then
    if cfn-lint cfn/*.yaml; then
        pass "cfn-lint"
    else
        fail "cfn-lint"
    fi
else
    skip "cfn-lint (not installed: python3 -m pip install cfn-lint)"
fi

# 7) Repo hygiene --------------------------------------------------------------
# Things that should never reach a public sample.
section "Hygiene"
# Matches ANY 12-digit number, on a word boundary. Two flaws in the previous
# pattern ('[^0-9](7[0-9]{11})[^0-9]'), both of which made it pass on real IDs:
#   1. `7[0-9]{11}` only matched IDs starting with 7 - an accident of the account
#      this sample was developed in. Any other leading digit sailed through.
#   2. Requiring a non-digit on BOTH sides meant an ID at end-of-line never
#      matched at all, because there is no trailing character to consume. That is
#      the common case: "Account: 123456789012" with nothing after it.
# \b fixes both. The allowlist carries the AWS documentation placeholders.
ACCT_OK='123456789012|111122223333|222233334444|333344445555|444455556666'
ACCT_HITS="$(grep -rInE '\b[0-9]{12}\b' --include='*.py' --include='*.sh' \
        --include='*.yaml' --include='*.sql' --include='*.md' . 2>/dev/null \
        | grep -vE "${ACCT_OK}" | grep -v tests/ | grep -v run-checks.sh)"
if [[ -n "${ACCT_HITS}" ]]; then
    printf '%s\n' "${ACCT_HITS}"
    fail "a real-looking 12-digit account ID is present (use 123456789012)"
else
    pass "no real account IDs"
fi
# Terms assembled from fragments so this script does not match its own pattern.
TERMS="$(printf 'white%s|black%s|mas%s|sla%s' list list ter ve)"
if grep -rIniE "\\b(${TERMS})\\b" \
        --include='*.py' --include='*.sh' --include='*.yaml' --include='*.sql' \
        --include='*.md' . 2>/dev/null \
        | grep -viE 'KMSMasterKeyId|MasterKeyId' \
        | grep -v 'run-checks.sh' >/dev/null; then
    grep -rIniE "\\b(${TERMS})\\b" \
        --include='*.py' --include='*.sh' --include='*.yaml' --include='*.sql' \
        --include='*.md' . 2>/dev/null \
        | grep -viE 'KMSMasterKeyId|MasterKeyId' | grep -v 'run-checks.sh'
    fail "non-inclusive terminology"
else
    pass "inclusive language"
fi

# Summary ----------------------------------------------------------------------
section "Summary"
for s in "${SKIPPED[@]+"${SKIPPED[@]}"}"; do printf '  skipped: %s\n' "$s"; done
if [[ "${FAILED}" -eq 0 ]]; then
    printf '\nAll checks passed.\n'
else
    printf '\nOne or more checks FAILED.\n' >&2
fi
exit "${FAILED}"
