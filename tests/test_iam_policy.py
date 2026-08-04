"""QuickSight inline-policy rendering.

Two real defects. First, the bucket name was interpolated straight into an IAM
Resource ARN with no validation, so `KIRO_LOGS_BUCKET='*'` (or `s3://*`, which an
unquoted shell glob can produce) rendered `arn:aws:s3:::*` and `arn:aws:s3:::*/*`
- granting the QuickSight service role read on every object in the account. That
grant is worse than a deploy-time slip: it lands on a role assumed by QuickSight
rather than by the deployer, it outlives the deploy, and it is shared by every
QuickSight principal in the account.

Second, the policy is written with put_role_policy (a wholesale replace) onto a
role every deployment shares, so the name must be namespaced per deployment or
one stack silently strips another's grants.
"""
from __future__ import annotations

import re
import unittest

from _helpers import SCRIPTS, load

g = load("grant_quicksight_s3")
DEPLOY = (SCRIPTS / "deploy.sh").read_text()
TEARDOWN = (SCRIPTS / "teardown.sh").read_text()


class TestBucketNameValidation(unittest.TestCase):

    def test_wildcards_are_rejected(self):
        """Each of these once produced an account-wide grant."""
        for bad in ("*", "s3://*", "arn:aws:s3:::*", "my-bucket/*", "**"):
            with self.subTest(value=bad):
                # deploy.sh strips the arn:/s3:// prefixes before this point, so
                # test the post-strip form too.
                stripped = bad.removeprefix("arn:aws:s3:::").removeprefix("s3://")
                with self.assertRaises(SystemExit):
                    g.parse_bucket_specs([f"{stripped}:read"])

    def test_malformed_names_are_rejected(self):
        # Not an exhaustive S3-naming implementation: the validator's job is to
        # stop a value that WIDENS the rendered policy. `a..b` is invalid to S3
        # but renders a harmlessly-scoped ARN and is rejected by AWS at deploy
        # time, so it is deliberately not enforced here.
        for bad in ("", "ab", "a" * 64, "-lead", "trail-", "MY-Bucket",
                    "my bucket", "a/b"):
            with self.subTest(value=bad):
                with self.assertRaises(SystemExit):
                    g.parse_bucket_specs([f"{bad}:read"])

    def test_real_bucket_names_are_accepted(self):
        """A false rejection would break every deploy, so this matters as much
        as the rejections."""
        for good in ("kiro-analytics-data-athena-results-123456789012-us-east-1",
                     "my-kiro-exports", "a.b.c-123", "abc"):
            with self.subTest(value=good):
                g.parse_bucket_specs([f"{good}:read"])

    def test_unknown_access_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            g.parse_bucket_specs(["valid-bucket-name:admin"])


class TestRenderedPolicy(unittest.TestCase):

    def _resources(self, pol):
        out = set()
        for st in pol["Statement"]:
            r = st["Resource"]
            out.update(r if isinstance(r, list) else [r])
        return out

    def test_no_object_wildcard_for_a_valid_bucket(self):
        pol = g.render_policy(g.parse_bucket_specs(["my-bucket:read"]))
        self.assertNotIn("arn:aws:s3:::*/*", self._resources(pol))

    def test_read_mode_grants_no_write(self):
        pol = g.render_policy(g.parse_bucket_specs(["my-bucket:read"]))
        actions = {a for st in pol["Statement"]
                   for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])}
        for w in ("s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"):
            self.assertNotIn(w, actions)

    def test_no_delete_action_is_ever_granted(self):
        """The solution never deletes from a customer bucket - in IAM as well as
        in code."""
        pol = g.render_policy(g.parse_bucket_specs(["a-bucket:read", "b-bucket:read_write"]))
        blob = str(pol)
        self.assertNotIn("s3:Delete", blob)

    def test_statement_sids_are_alphanumeric_and_unique(self):
        """IAM rejects a non-alphanumeric Sid; a duplicate silently overwrites."""
        pol = g.render_policy(g.parse_bucket_specs(
            ["a.b.c-123:read", "my-bucket:read_write"]))
        sids = [st["Sid"] for st in pol["Statement"] if "Sid" in st]
        self.assertEqual(len(sids), len(set(sids)), "duplicate Sid")
        for s in sids:
            with self.subTest(sid=s):
                self.assertTrue(s.isalnum(), f"non-alphanumeric Sid: {s}")

    def test_kms_grant_is_decrypt_only(self):
        pol = g.render_policy(g.parse_bucket_specs(["my-bucket:read"]),
                              ["arn:aws:kms:us-east-1:1:key/abc"])
        kms = [st for st in pol["Statement"] if "kms" in str(st["Action"])]
        self.assertTrue(kms)
        for st in kms:
            self.assertEqual(st["Action"], ["kms:Decrypt"])


class TestPolicyCoversSemantics(unittest.TestCase):
    """policy_covers is additive by design so parallel deployments do not fight.
    That also means a NARROWING re-apply is a no-op - which is why the opt-out
    path must revoke before re-applying."""

    def _p(self, *specs, kms=None):
        return g.render_policy(g.parse_bucket_specs(list(specs)), kms)

    def test_identical_policy_is_covered(self):
        p = self._p("a-bucket:read")
        self.assertTrue(g.policy_covers(p, p))

    def test_superset_covers_a_subset(self):
        self.assertTrue(g.policy_covers(self._p("a-bucket:read", "b-bucket:read"),
                                        self._p("a-bucket:read")))

    def test_subset_does_not_cover_a_superset(self):
        self.assertFalse(g.policy_covers(self._p("a-bucket:read"),
                                         self._p("a-bucket:read", "b-bucket:read")))

    def test_read_does_not_cover_read_write(self):
        self.assertFalse(g.policy_covers(self._p("a-bucket:read"),
                                         self._p("a-bucket:read_write")))

    def test_narrowing_is_not_detected_hence_revoke_before_reapply(self):
        """Pins the reason deploy.sh revokes before re-applying on opt-out: the
        narrower policy is already 'covered', so re-applying alone would leave
        the identity-map grants in place."""
        wide = self._p("a-bucket:read", "pii-bucket:read")
        narrow = self._p("a-bucket:read")
        self.assertTrue(g.policy_covers(wide, narrow),
                        "narrowing would be a silent no-op - revoke first")


class TestPolicyNameNamespacing(unittest.TestCase):
    """All deployments share one QuickSight service role, and the policy is
    written with a wholesale replace."""

    def test_deploy_and_teardown_derive_the_same_name(self):
        pat = r'QS_S3_POLICY_NAME="\$\{QS_S3_POLICY_NAME:-\$\{STACK_PREFIX\}-QuickSightS3Access\}"'
        self.assertRegex(DEPLOY, pat, "deploy.sh does not namespace the policy name")
        self.assertRegex(TEARDOWN, pat,
                         "teardown.sh derives a different name - it would revoke "
                         "the wrong deployment's policy, or nothing at all")

    def test_every_grant_invocation_passes_the_policy_name(self):
        """A call site that omits --policy-name writes the DEFAULT name, which
        re-introduces the cross-deployment clobber for that one path."""
        for script, body in (("deploy.sh", DEPLOY), ("teardown.sh", TEARDOWN)):
            # Count real INVOCATIONS (a `python3 .../grant_quicksight_s3.py` line),
            # not comment mentions of the filename.
            calls = len(re.findall(r"^\s*python3 .*grant_quicksight_s3\.py",
                                   body, re.M))
            passes = body.count('--policy-name "${QS_S3_POLICY_NAME}"')
            with self.subTest(script=script):
                self.assertEqual(
                    calls, passes,
                    f"{script}: {calls} grant_quicksight_s3.py invocations but "
                    f"{passes} pass --policy-name",
                )

    def test_default_name_is_not_used_by_the_scripts(self):
        self.assertNotIn("KiroAnalyticsQuickSightS3Access", DEPLOY)
        self.assertNotIn("KiroAnalyticsQuickSightS3Access", TEARDOWN)


if __name__ == "__main__":
    unittest.main()
