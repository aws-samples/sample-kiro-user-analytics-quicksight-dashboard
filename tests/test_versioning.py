"""Version stamping: the tag must reach every stack, and must not destroy the
customer's own tags on the way.

The second half is why this file exists. `aws cloudformation deploy --tags`
REPLACES a stack's entire tag set rather than merging into it - verified against a
live stack, where deploying with only our version tag wiped pre-existing
CostCenter and Team tags. So the naive way to add a version tag silently deletes
customers' cost-allocation and ownership tags on their next upgrade, and the
damage shows up weeks later in a billing report with nothing pointing back here.

deploy.sh therefore reads the existing tags and re-sends them alongside ours.
That merge is shell string handling, so it is tested by EXECUTING the real
function lifted out of deploy.sh - not by grepping for it - with `aws` stubbed to
return canned describe-stacks output.
"""
from __future__ import annotations

import re
import subprocess
import unittest

from _helpers import CFN, REPO, SCRIPTS

DEPLOY = (SCRIPTS / "deploy.sh").read_text()
PREFLIGHT = (SCRIPTS / "preflight.sh").read_text()
CREATE = (SCRIPTS / "create_dashboard.py").read_text()
TAG_KEY = "KiroAnalyticsVersion"


def extract_function(script: str, name: str) -> str:
    """Lift a shell function body out of a script by brace matching.

    Runs the SHIPPING code rather than a copy, so a change to deploy.sh that
    breaks the merge fails here instead of passing against a stale duplicate.
    """
    start = script.index(f"{name}() {{")
    depth, i = 0, start
    while True:
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
        i += 1


class TestVersionFile(unittest.TestCase):

    def test_version_file_exists_and_is_semver(self):
        raw = (REPO / "VERSION").read_text()
        self.assertRegex(raw.strip(), r"^\d+\.\d+\.\d+$",
                         "VERSION must be a bare MAJOR.MINOR.PATCH string")

    def test_version_file_is_a_single_line(self):
        """deploy.sh reads it with `tr -d '[:space:]'`; a stray second line
        would silently concatenate into the tag value."""
        self.assertEqual(len((REPO / "VERSION").read_text().strip().splitlines()), 1)

    def test_changelog_documents_the_current_version(self):
        changelog = (REPO / "CHANGELOG.md").read_text()
        version = (REPO / "VERSION").read_text().strip()
        self.assertIn(f"[{version}]", changelog,
                      f"CHANGELOG.md has no entry for the current VERSION ({version})")


def deploy_invocations() -> list[tuple[int, str]]:
    """Every real `aws cloudformation deploy` COMMAND in deploy.sh, as
    (line index, full command text).

    Line-based and comment-skipping on purpose: a naive search for the string
    also matches the comment block that explains --tags replacement semantics,
    which made an earlier version of this test fail against prose.
    """
    lines = DEPLOY.splitlines()
    out = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("aws cloudformation deploy"):
            continue
        # Follow backslash continuations to capture the whole command.
        end = i
        while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
            end += 1
        out.append((i, "\n".join(lines[i:end + 1])))
    return out


class TestEveryStackIsTagged(unittest.TestCase):
    """An untagged stack is an unidentifiable deployment - the exact problem
    versioning is here to solve."""

    def test_all_four_deploy_calls_are_found(self):
        """Guards the parsing the two tests below depend on: data, data-retry,
        identity-map, quicksight."""
        self.assertEqual(len(deploy_invocations()), 4)

    def test_every_cloudformation_deploy_passes_tags(self):
        for lineno, cmd in deploy_invocations():
            stack = re.search(r"--stack-name \"\$\{([A-Z_]+)\}\"", cmd)
            with self.subTest(stack=stack.group(1) if stack else f"line {lineno}"):
                self.assertIn('--tags "${STACK_TAGS[@]}"', cmd,
                              "deploy call does not pass the merged tag set")

    def test_tag_args_are_rebuilt_before_each_deploy(self):
        """STACK_TAGS is per-stack: reusing the previous stack's array would
        copy that stack's tags onto this one."""
        lines = DEPLOY.splitlines()
        for lineno, _ in deploy_invocations():
            window = "\n".join(lines[max(0, lineno - 4):lineno])
            with self.subTest(line=lineno + 1):
                self.assertIn("stack_tag_args", window,
                              "no stack_tag_args call in the 4 lines before "
                              "this deploy")


class TestTagMerge(unittest.TestCase):
    """Execute the real merge helper with `aws` stubbed."""

    def run_merge(self, fake_aws_output: str) -> list[str]:
        """FAKE is the stack's FULL tag set, as describe-stacks --output text
        would render it (one tab-separated Key/Value pair per line).

        The stub honours the JMESPath `Key!=` exclusion rather than returning
        FAKE blindly, so the helper's reliance on that filter is actually
        exercised: drop the filter from deploy.sh and the duplicate-tag test
        below fails, which is the behaviour CloudFormation would reject.
        """
        fn = extract_function(DEPLOY, "stack_tag_args")
        script = f"""
set -euo pipefail
REGION="us-east-1"
VERSION="9.9.9"
aws() {{
    if [[ "$*" == *"Key!='{TAG_KEY}'"* ]]; then
        printf '%s' "$FAKE" | grep -v "^{TAG_KEY}$(printf '\\t')" || true
    else
        printf '%s' "$FAKE"
    fi
}}
{fn}
stack_tag_args "some-stack"
for t in "${{STACK_TAGS[@]}}"; do printf '%s\\n' "$t"; done
"""
        out = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, check=True,
            env={"FAKE": fake_aws_output, "PATH": "/usr/bin:/bin"},
        )
        return out.stdout.splitlines()

    def test_version_tag_is_always_present(self):
        for desc, fake in (("no tags", ""), ("literal None", "None"),
                           ("one tag", "CostCenter\tcc-1234")):
            with self.subTest(case=desc):
                self.assertIn(f"{TAG_KEY}=9.9.9", self.run_merge(fake))

    def test_existing_customer_tags_survive(self):
        """The regression this file exists for."""
        got = self.run_merge("CostCenter\tcc-1234\nTeam\tfinops")
        self.assertIn("CostCenter=cc-1234", got)
        self.assertIn("Team=finops", got)
        self.assertIn(f"{TAG_KEY}=9.9.9", got)

    def test_values_containing_spaces_survive(self):
        """Spaces are legal in CFN tag values (verified live), so naive word
        splitting would corrupt them into separate tags."""
        got = self.run_merge("CostCenter\tcc 1234\nOwner\tPlatform Eng Team")
        self.assertIn("CostCenter=cc 1234", got)
        self.assertIn("Owner=Platform Eng Team", got)

    def test_keys_containing_spaces_survive(self):
        """CFN allows spaces in tag KEYS too, and "Cost Center" is a common
        cost-allocation tag (verified live). This is the case that separates a
        tab-delimited read from a whitespace-delimited one: plain
        `read -r key value` on "Cost Center<TAB>cc 1234" yields key="Cost",
        value="Center<TAB>cc 1234" - inventing a tag and destroying the real
        one. Found by mutating the IFS out of the shipping code and noticing
        the suite stayed green."""
        got = self.run_merge("Cost Center\tcc 1234")
        self.assertIn("Cost Center=cc 1234", got)
        self.assertEqual(len(got), 2, f"expected exactly 2 tags, got {got}")

    def test_padded_values_are_not_trimmed(self):
        """Whitespace-delimited reads also silently strip leading/trailing
        spaces, which changes the tag CFN stores."""
        self.assertIn("Note=  padded", self.run_merge("Note\t  padded"))

    def test_equals_in_a_value_is_preserved(self):
        """'=' is legal in a tag VALUE, so the split must be on the first '='
        only - CFN itself parses `a=b=c` as key 'a', value 'b=c'."""
        self.assertIn("a=b=c", self.run_merge("a\tb=c"))

    def test_no_duplicate_version_tag_on_upgrade(self):
        """The upgrade path: the stack already carries an OLDER version tag.
        It must be replaced, not re-sent alongside the new one - two values for
        one key is a CloudFormation error, which would break every upgrade."""
        got = self.run_merge(f"{TAG_KEY}\t0.9.0\nCostCenter\tcc-1234")
        self.assertEqual(sum(1 for t in got if t.startswith(f"{TAG_KEY}=")), 1,
                         f"expected exactly one version tag, got {got}")
        self.assertIn(f"{TAG_KEY}=9.9.9", got, "kept the stale version")
        self.assertNotIn(f"{TAG_KEY}=0.9.0", got, "re-sent the stale version")
        self.assertIn("CostCenter=cc-1234", got)

    def test_the_query_excludes_our_own_tag_key(self):
        """The dedup above relies on the describe-stacks JMESPath filter."""
        fn = extract_function(DEPLOY, "stack_tag_args")
        self.assertIn(f"Key!='{TAG_KEY}'", fn)

    def test_blank_lines_do_not_become_empty_tags(self):
        """An empty tag value is rejected by the API, so a stray blank line
        would fail the whole deploy rather than be ignored."""
        got = self.run_merge("CostCenter\tcc-1234\n\n")
        self.assertNotIn("=", got[-1].removeprefix("CostCenter="))
        self.assertEqual([t for t in got if t.startswith("=")], [])


class TestDashboardVersionDescription(unittest.TestCase):

    def test_version_reaches_create_dashboard(self):
        self.assertIn('--version "${VERSION}"', DEPLOY,
                      "deploy.sh does not pass --version to create_dashboard.py")

    def test_version_description_is_dashboard_only(self):
        """VersionDescription exists on Create/UpdateDashboard but NOT on the
        Analysis operations (checked against the botocore service model), so
        setting it unconditionally would raise ParamValidationError on the
        analysis path - after the dashboard had already been written."""
        self.assertRegex(CREATE, r"if version and is_dashboard:")

    def test_version_description_respects_the_api_length_cap(self):
        self.assertIn("[:512]", CREATE,
                      "VersionDescription is capped at 512 chars by the API")


class TestPreflightReportsVersion(unittest.TestCase):

    def test_preflight_reads_both_versions(self):
        self.assertIn("VERSION", PREFLIGHT)
        self.assertIn(f"Key=='{TAG_KEY}'", PREFLIGHT,
                      "preflight does not read the deployed version tag")

    def test_preflight_handles_an_untagged_deployment(self):
        """Deployments predating versioning have no tag; preflight must say so
        rather than print an empty version."""
        self.assertIn("CHANGELOG.md", PREFLIGHT)


class TestDocumentation(unittest.TestCase):

    def test_readme_has_an_upgrading_section(self):
        readme = (REPO / "README.md").read_text()
        self.assertIn("## Upgrading", readme)

    def test_readme_warns_that_spice_must_refresh(self):
        """The most common "I upgraded and nothing changed" cause: views are
        rebuilt correctly but SPICE still serves the old import."""
        readme = (REPO / "README.md").read_text()
        upgrading = readme.split("## Upgrading", 1)[1].split("\n## ", 1)[0]
        self.assertIn("SPICE", upgrading)


if __name__ == "__main__":
    unittest.main()
