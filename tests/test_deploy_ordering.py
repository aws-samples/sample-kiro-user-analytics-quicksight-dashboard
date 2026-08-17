"""Ordering invariants in deploy.sh that only bite on a FIRST deploy.

A step placed before the thing it depends on still works on every re-deploy,
because the earlier run left the dependency behind. It fails only on a fresh
install - the one path a developer re-running their own stack never exercises,
and the only path a new customer ever takes. That asymmetry is why these are
worth pinning in a test rather than trusting to review.

Concretely, the bug this file was written for: CloudWatch log retention was set
once, BEFORE the synchronous normalizer invoke. But Lambda only auto-creates the
log group when the function first runs, so on a first deploy the group did not
exist yet, the call failed, and retention was never retried - leaving Lambda's
default of "never expires" on a brand-new deployment. Found by deploying to a
fresh stack and reading back retentionInDays: None. Every re-deploy looked fine,
because by then the group existed.
"""
from __future__ import annotations

import re
import unittest

from _helpers import SCRIPTS

DEPLOY = (SCRIPTS / "deploy.sh").read_text()


def line_of(pattern: str, *, after: int = 0) -> int:
    """1-indexed line number of the first non-comment line matching pattern."""
    for i, line in enumerate(DEPLOY.splitlines()[after:], start=after + 1):
        if line.strip().startswith("#"):
            continue
        if re.search(pattern, line):
            return i
    raise AssertionError(f"pattern not found in deploy.sh: {pattern}")


class TestLogRetentionSurvivesAFirstDeploy(unittest.TestCase):

    def test_retention_is_attempted_after_the_invoke_that_creates_the_group(self):
        """The load-bearing assertion. Lambda creates the log group on first
        invocation, so a retention call that only runs BEFORE the invoke can
        never succeed on a fresh deployment."""
        invoke = line_of(r"aws lambda invoke .*|--function-name \"\$\{NORMALIZER_LAMBDA\}\"")
        # Exclude the DEFINITION line: `set_log_retention() {` also matches a
        # bare-name regex, and counting it as a call would let this test pass
        # vacuously if the definition were moved below the invoke and every real
        # call left above it.
        calls = [i for i, line in enumerate(DEPLOY.splitlines(), start=1)
                 if re.match(r"\s*(if !\s*)?set_log_retention\b", line)
                 and "set_log_retention() {" not in line
                 and not line.strip().startswith("#")]
        self.assertTrue(calls, "deploy.sh never calls set_log_retention")
        self.assertTrue(
            any(c > invoke for c in calls),
            f"set_log_retention is only called before the normalizer invoke "
            f"(line {invoke}); on a first deploy the log group does not exist "
            f"yet, so retention would never be set. Calls at: {calls}",
        )

    def test_retention_is_a_function_not_a_duplicated_block(self):
        """It runs twice; two copies of the logic would drift."""
        self.assertIn("set_log_retention() {", DEPLOY)
        self.assertEqual(DEPLOY.count("set_log_retention() {"), 1)

    def test_the_first_attempt_cannot_abort_the_deploy(self):
        """deploy.sh runs under `set -e`. The pre-invoke attempt is EXPECTED to
        fail on a first deploy, so it must be neutralised or it would kill the
        run before anything is provisioned."""
        first = line_of(r"^set_log_retention \|\| true|^\s*set_log_retention \|\| true")
        self.assertGreater(first, 0)

    def test_retention_uses_the_configured_value(self):
        """A hardcoded number would silently ignore LOG_RETENTION_DAYS."""
        block = DEPLOY.split("set_log_retention() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("${LOG_RETENTION_DAYS}", block)
        self.assertNotRegex(block, r"--retention-in-days \d")


class TestNormalizerRunsBeforeViewsAreBuilt(unittest.TestCase):
    """build_views registers external tables over the normalizer's output. If the
    views were built first, a first deploy would create tables over a prefix that
    does not exist yet."""

    def test_normalizer_invoke_precedes_the_view_build(self):
        invoke = line_of(r"--function-name \"\$\{NORMALIZER_LAMBDA\}\"")
        tables = line_of(r"build_views\.py")
        self.assertLess(invoke, tables,
                        "views are built before the normalizer has written any "
                        "output; on a first deploy the tables would be empty")


class TestIdentityMapOrdering(unittest.TestCase):
    """The identity-map Lambda queries report_facts for distinct users, so the
    external tables must exist first; and build_views must run again afterwards
    to pick up the identity join."""

    def test_tables_only_build_precedes_identity_mapping(self):
        tables_only = line_of(r"--tables-only")
        idmap = line_of(r"Deploying \$\{IDMAP_STACK\}|IDMAP_STACK\}\"")
        self.assertLess(tables_only, idmap,
                        "identity mapping runs before report_facts exists")


if __name__ == "__main__":
    unittest.main()
