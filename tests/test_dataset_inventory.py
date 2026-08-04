"""deploy.sh's dataset inventories must match the CloudFormation template.

This is the highest-value test in the suite, because the drift it detects caused a
live PRIVACY failure. Two hardcoded lists in deploy.sh drove the
identity-mapping refresh and the opt-out purge. Both had silently fallen out of
step with cfn/02-quicksight.yaml: the purge list still named a deleted dataset (a
404 swallowed by `|| echo`) and BOTH lists omitted `user-daily-dense`, which
carries `user_label`.

The consequence was that `IDENTITY_MAPPING=false` printed "Identity mapping fully
removed", exited 0, and left resolved real names in that dataset's SPICE data -
with the identity-map table and PII bucket already deleted, so nothing signalled
the residue. The dataset powers the User-detail sheet, so the names stayed
visible.

A list that drifts is invisible in review. Comparing it to its source of truth is
25 lines of test.
"""
from __future__ import annotations

import re
import unittest

from _helpers import CFN, SCRIPTS

TEMPLATE = (CFN / "02-quicksight.yaml").read_text()
DEPLOY = (SCRIPTS / "deploy.sh").read_text()


def template_datasets() -> set[str]:
    """Dataset id suffixes declared as AWS::QuickSight::DataSet resources."""
    return set(re.findall(r'DataSetId: !Sub "\$\{ResourcePrefix\}-([a-z-]+)"', TEMPLATE))


def template_label_datasets() -> set[str]:
    """The subset whose InputColumns include user_label - i.e. can hold PII."""
    out = set()
    for block in re.split(r"\n  (?=[A-Z]\w+DataSet:)", TEMPLATE):
        m = re.search(r'DataSetId: !Sub "\$\{ResourcePrefix\}-([a-z-]+)"', block)
        if m and "user_label" in block:
            out.add(m.group(1))
    return out


def bash_array(name: str) -> set[str]:
    m = re.search(rf"{name}=\((.*?)\)", DEPLOY, re.S)
    if not m:
        raise AssertionError(f"{name} not found in deploy.sh")
    return set(m.group(1).split())


class TestDatasetInventories(unittest.TestCase):

    def test_all_datasets_list_matches_the_template(self):
        declared, listed = template_datasets(), bash_array("QS_ALL_DATASETS")
        self.assertEqual(
            listed, declared,
            f"QS_ALL_DATASETS drifted.\n"
            f"  ghosts (in list, not in template): {sorted(listed - declared)}\n"
            f"  missing (in template, not listed): {sorted(declared - listed)}",
        )

    def test_label_datasets_list_matches_the_template(self):
        declared, listed = template_label_datasets(), bash_array("QS_LABEL_DATASETS")
        self.assertEqual(
            listed, declared,
            f"QS_LABEL_DATASETS drifted - a dataset carrying user_label that is "
            f"NOT in this list will retain resolved names after opt-out.\n"
            f"  ghosts:  {sorted(listed - declared)}\n"
            f"  missing: {sorted(declared - listed)}",
        )

    def test_label_datasets_is_a_subset_of_all_datasets(self):
        self.assertLessEqual(bash_array("QS_LABEL_DATASETS"), bash_array("QS_ALL_DATASETS"))

    def test_every_dataset_has_a_refresh_schedule(self):
        """A dataset without a schedule goes stale silently after its initial
        ingestion."""
        scheduled = set(re.findall(
            r'RefreshSchedule[\s\S]{0,400}?DataSetId: !Sub "\$\{ResourcePrefix\}-([a-z-]+)"',
            TEMPLATE))
        missing = template_datasets() - scheduled
        self.assertEqual(missing, set(), f"no refresh schedule for: {sorted(missing)}")

    def test_no_dataset_is_declared_but_unused_by_the_dashboard(self):
        """Two datasets once refreshed nightly while feeding zero visuals -
        paying for Athena scans and SPICE capacity for nothing."""
        from _helpers import load
        import json
        cd = load("create_dashboard")
        for mapping in (False, True):
            d = cd.build_definition("123456789012", "us-east-1", "p", identity_mapping=mapping)
            blob = json.dumps(d)
            unused = [x["Identifier"] for x in d["DataSetIdentifierDeclarations"]
                      if f'"DataSetIdentifier": "{x["Identifier"]}"' not in blob]
            with self.subTest(identity_mapping=mapping):
                self.assertEqual(unused, [], f"declared but never referenced: {unused}")

    def test_dashboard_only_references_datasets_the_template_creates(self):
        from _helpers import load
        cd = load("create_dashboard")
        for mapping in (False, True):
            d = cd.build_definition("123456789012", "us-east-1", "p", identity_mapping=mapping)
            phys = {re.search(r"dataset/p-([a-z-]+)$", x["DataSetArn"]).group(1)
                    for x in d["DataSetIdentifierDeclarations"]}
            with self.subTest(identity_mapping=mapping):
                self.assertLessEqual(
                    phys, template_datasets(),
                    f"dashboard references datasets the template does not create: "
                    f"{sorted(phys - template_datasets())}",
                )


if __name__ == "__main__":
    unittest.main()
