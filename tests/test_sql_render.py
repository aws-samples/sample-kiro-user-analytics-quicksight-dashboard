"""Every view must render, split cleanly, and leave no placeholder behind.

Two real bugs live here. First, a `;` at the end of a line inside a `--` comment
truncated a view mid-statement, producing a baffling `mismatched input '<EOF>'`
from Athena - prose is not a statement boundary. Second, a placeholder added to
one code path but not the other renders literally into the SQL and fails at
deploy time against a customer's Athena rather than here.
"""
from __future__ import annotations

import re
import string
import unittest

from _helpers import ATHENA, load, view_substitutions

bv = load("build_views")


class TestViewRendering(unittest.TestCase):

    def _render(self, sql_file, *, identity_mapping):
        subs = view_substitutions(bv, identity_mapping=identity_mapping)
        return string.Template(sql_file.read_text()).substitute(**subs)

    def test_every_view_renders_in_both_identity_modes(self):
        """string.Template.substitute raises KeyError on an unknown placeholder,
        so this catches a placeholder added to a .sql file but not to the
        renderer - in either branch."""
        files = sorted(ATHENA.glob("*.sql"))
        self.assertGreater(len(files), 0, "no view files found")
        for sql_file in files:
            for mapping in (False, True):
                with self.subTest(view=sql_file.name, identity_mapping=mapping):
                    self._render(sql_file, identity_mapping=mapping)

    def test_no_unsubstituted_placeholder_survives(self):
        for sql_file in sorted(ATHENA.glob("*.sql")):
            for mapping in (False, True):
                rendered = self._render(sql_file, identity_mapping=mapping)
                # `$path` is an Athena pseudo-column, written `$$path` in the
                # source so Template escapes it to a literal `$path`. That is
                # intentional, not an unsubstituted placeholder.
                leftover = re.findall(r"\$\{?\w+\}?", rendered.replace("$path", ""))
                with self.subTest(view=sql_file.name, identity_mapping=mapping):
                    self.assertEqual(leftover, [], f"unsubstituted: {leftover}")

    def test_each_view_file_is_exactly_one_statement(self):
        """Athena accepts one statement per query. More than one here means the
        splitter mis-parsed the file."""
        for sql_file in sorted(ATHENA.glob("*.sql")):
            rendered = self._render(sql_file, identity_mapping=False)
            with self.subTest(view=sql_file.name):
                self.assertEqual(len(bv.split_statements(rendered)), 1)

    def test_every_view_declares_create_or_replace(self):
        """CREATE OR REPLACE keeps re-deploys idempotent."""
        for sql_file in sorted(ATHENA.glob("*.sql")):
            with self.subTest(view=sql_file.name):
                self.assertIn("CREATE OR REPLACE VIEW", sql_file.read_text())


class TestStatementSplitter(unittest.TestCase):

    def test_semicolon_inside_a_comment_is_not_a_boundary(self):
        """The original bug: a ';' ending a COMMENT line truncated the view."""
        sql = ("-- a comment that ends with a semicolon (often empty);\n"
               "SELECT 1\nFROM t;\n")
        self.assertEqual(len(bv.split_statements(sql)), 1)

    def test_multiple_real_statements_split(self):
        self.assertEqual(len(bv.split_statements("DROP VIEW a;\nCREATE VIEW a AS SELECT 1;\n")), 2)

    def test_trailing_statement_without_a_semicolon_is_kept(self):
        self.assertEqual(len(bv.split_statements("SELECT 1")), 1)

    def test_blank_input_yields_nothing(self):
        self.assertEqual(bv.split_statements("\n\n  \n"), [])


class TestDateCastSafety(unittest.TestCase):
    """One unparseable date cell must not fail nine downstream views."""

    def test_date_casts_are_guarded(self):
        for sql_file in sorted(ATHENA.glob("*.sql")):
            body = sql_file.read_text()
            for m in re.finditer(r"(?<!TRY\()CAST\(\s*([\w.]*date)\s+AS\s+date\s*\)",
                                 body, re.I):
                # Allow it if a TRY( immediately precedes the match.
                if body[max(0, m.start() - 4):m.start()] == "TRY(":
                    continue
                with self.subTest(view=sql_file.name, expr=m.group(0)):
                    self.fail(f"{sql_file.name}: unguarded {m.group(0)} - use TRY(CAST(...))")


class TestIdentityRendering(unittest.TestCase):

    def test_label_is_per_user_constant_in_both_modes(self):
        """user_label keys DrillUser and the All-users grouping, so it must not
        vary across a user's rows. Built from the per-ROW `email` column, it
        split 348 of 385 simulated users into multiple table rows."""
        for mapping in (False, True):
            parts = bv.render_identity_label_parts("db", "email", mapping)
            with self.subTest(identity_mapping=mapping):
                self.assertIn("OVER (PARTITION BY userid)", parts["base_user_label"],
                              "base_user_label is not collapsed per user")

    def test_identity_columns_are_typed_nulls_when_mapping_is_off(self):
        """The QuickSight DataSet declares these unconditionally; a missing
        column breaks SPICE ingestion."""
        parts = bv.render_identity_label_parts("db", "email", False)
        for k in ("dim_idc_username", "dim_idc_email",
                  "base_idc_username", "base_idc_email"):
            with self.subTest(key=k):
                self.assertEqual(parts[k], "CAST(NULL AS varchar)")

    def test_hash_emails_and_identity_mapping_are_mutually_exclusive(self):
        """Resolving names while hashing email is contradictory, and would write
        a plaintext display_name beside a hashed address."""
        self.assertIn("sha256", bv.email_expression(True))
        self.assertEqual(bv.email_expression(False), "email")

    def test_identity_join_is_collapsed_to_one_row_per_user(self):
        """The external table's LOCATION is a prefix, so a stray object would
        duplicate an idc_user_id and fan a user's SUMs out across rows."""
        src = bv._identity_map_source("db")
        self.assertIn("GROUP BY idc_user_id", src)


if __name__ == "__main__":
    unittest.main()
