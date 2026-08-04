"""View files must sort into dependency order.

build_views.py applies `sorted(VIEWS_DIR.glob("*.sql"))`, so a view's position in
the filename sort IS its position in the dependency graph - and most views read
another view. Nothing declares that dependency, which makes the ordering a silent
contract.

The failure signature is the nastiest kind: adding `00a_something.sql` that reads
`base_user_activity` (created by `00_base_user_activity.sql`, which sorts AFTER
it) fails on a CLEAN deploy but succeeds on a RE-deploy, because the second time
round the referenced view already exists from the previous run. A contributor
would see it pass locally and break for a new customer.
"""
from __future__ import annotations

import re
import unittest

from _helpers import ATHENA, load

bv = load("build_views")

# Created by build_views.report_tables_ddl before any .sql file is applied.
PRE_EXISTING = {"report_facts", "report_models", "report_facts_raw",
                "report_models_raw", "identity_map"}


def view_graph():
    """[(filename, created_view, {views_it_reads})] in application order."""
    graph = []
    for f in sorted(ATHENA.glob("*.sql")):
        body = f.read_text()
        m = re.search(r"CREATE OR REPLACE VIEW \$\{database\}\.(\w+)", body)
        reads = set(re.findall(r"(?:FROM|JOIN)\s+\$\{database\}\.(\w+)", body))
        graph.append((f.name, m.group(1) if m else None, reads))
    return graph


class TestViewApplicationOrder(unittest.TestCase):

    def test_every_view_is_created_before_it_is_read(self):
        created = {}
        for pos, (name, view, reads) in enumerate(view_graph()):
            for dep in reads:
                if dep in PRE_EXISTING:
                    continue
                with self.subTest(view=name, depends_on=dep):
                    self.assertIn(
                        dep, created,
                        f"{name} reads {dep}, which no earlier file creates. "
                        f"Filename sort order IS dependency order here - rename "
                        f"so {dep}'s file sorts first.",
                    )
            if view:
                created[view] = pos

    def test_every_referenced_relation_exists_somewhere(self):
        """Catches a view left behind after its dependency was deleted."""
        graph = view_graph()
        all_created = {v for _, v, _ in graph if v} | PRE_EXISTING
        for name, _, reads in graph:
            for dep in reads:
                with self.subTest(view=name, depends_on=dep):
                    self.assertIn(dep, all_created,
                                  f"{name} reads {dep}, which nothing creates")

    def test_no_view_reads_itself(self):
        for name, view, reads in view_graph():
            with self.subTest(view=name):
                self.assertNotIn(view, reads, f"{name} references itself")

    def test_every_file_creates_exactly_one_view(self):
        """One statement per file keeps the ordering contract legible."""
        for name, view, _ in view_graph():
            with self.subTest(view=name):
                self.assertIsNotNone(view, f"{name} creates no view")

    def test_deleted_views_are_not_still_referenced(self):
        """engagement_segmentation and period_comparison were removed; nothing -
        including a stale comment pointing at a file that no longer exists -
        should still name them as a relation."""
        for name, _, reads in view_graph():
            for gone in ("engagement_segmentation", "period_comparison",
                         "cohort_retention", "engagement_funnel"):
                with self.subTest(view=name, removed=gone):
                    self.assertNotIn(gone, reads)


class TestIdentifierValidation(unittest.TestCase):
    """The database name is interpolated into SQL, so its validator is a
    security control as well as a correctness one."""

    def test_injection_attempts_are_rejected(self):
        for bad in ("a'b", "a;b", "a b", 'a"b', "a\nb", "a--b", "", "1abc"):
            with self.subTest(value=bad):
                with self.assertRaises(Exception):
                    bv._validate_identifier(bad, "database")

    def test_ordinary_names_are_accepted(self):
        for good in ("kiro_analytics", "kiro_synthetic", "db1", "_x"):
            with self.subTest(value=good):
                bv._validate_identifier(good, "database")


if __name__ == "__main__":
    unittest.main()
