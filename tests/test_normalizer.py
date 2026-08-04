"""Report-normalizer parsing invariants.

The normalizer exists because Kiro's CSV header drifts: a new `<model>_messages`
column appears whenever Kiro launches a model, and the trailing `New_User` column
is present in some exports and absent in others. A positional reader silently
misattributed per-model usage and corrupted `new_user` - the bug that motivated
this whole component. These tests pin the header-keyed behaviour that fixes it,
plus the input-hardening that keeps one bad cell from aborting an entire run.
"""
from __future__ import annotations

import unittest

from _helpers import load

n = load("normalize_report_lambda")

KEY = "user_report/us-east-1/2026/07/01/00/KIRO_IDE_1_user_report_202607010000.csv"


def parse(body: str, key: str = KEY):
    return n._parse_file(body, key, "part-abc", "20260701030000")


def fact(row) -> dict:
    return dict(zip(n._FACT_OUT_HEADER, row))


def model_rows(rows) -> dict:
    """{model_name: messages} from the long-form model rows."""
    h = n._MODEL_OUT_HEADER
    return {r[h.index("model_name")]: r[h.index("messages")] for r in rows}


class TestHeaderDrift(unittest.TestCase):
    """Values must bind by header NAME, never by position."""

    def test_column_order_does_not_matter(self):
        a = "Date,UserId,Total_Messages,auto_messages\n2026-07-01,u1,10,7\n"
        # Same data, columns shuffled - the positional reader's failure case.
        b = "auto_messages,Total_Messages,UserId,Date\n7,10,u1,2026-07-01\n"
        fa, ma = parse(a)
        fb, mb = parse(b)
        self.assertEqual(fact(fa[0])["total_messages"], "10")
        self.assertEqual(fact(fa[0]), fact(fb[0]))
        self.assertEqual(model_rows(ma), model_rows(mb))

    def test_new_model_column_adds_rows_not_columns(self):
        """A model Kiro has never shipped before must not change the schema."""
        body = ("Date,UserId,Total_Messages,auto_messages,claude_opus_9.9_messages\n"
                "2026-07-01,u1,10,4,6\n")
        facts, models = parse(body)
        self.assertEqual(len(fact(facts[0])), len(n._FACT_OUT_HEADER))
        self.assertEqual(model_rows(models), {"auto": 4, "claude_opus_9.9": 6})

    def test_absent_new_user_column_is_not_misread(self):
        """New_User is present in some exports and absent in others."""
        with_col = "Date,UserId,auto_messages,New_User\n2026-07-01,u1,3,true\n"
        without = "Date,UserId,auto_messages\n2026-07-01,u1,3\n"
        self.assertEqual(fact(parse(with_col)[0][0])["new_user"], "true")
        # Absent must read as empty, NOT as whatever value sat in that position.
        self.assertEqual(fact(parse(without)[0][0])["new_user"], "")

    def test_header_case_and_whitespace_are_tolerated(self):
        body = " DATE , UserId ,Auto_Messages\n2026-07-01,u1,5\n"
        facts, models = parse(body)
        self.assertEqual(fact(facts[0])["date"], "2026-07-01")
        self.assertEqual(model_rows(models), {"auto": 5})

    def test_total_messages_is_not_treated_as_a_model(self):
        body = "Date,UserId,Total_Messages,auto_messages\n2026-07-01,u1,10,10\n"
        self.assertNotIn("total", model_rows(parse(body)[1]))

    def test_zero_message_models_are_dropped(self):
        """Long-form rows only carry models the user actually used."""
        body = "Date,UserId,auto_messages,claude_opus_4.8_messages\n2026-07-01,u1,5,0\n"
        self.assertEqual(model_rows(parse(body)[1]), {"auto": 5})


class TestMalformedInput(unittest.TestCase):
    """One bad cell must not abort the run. Historically some of these raised,
    and because the handler had no per-file guard, every later file in the run -
    and in every future run - was skipped."""

    def test_non_finite_counts_are_zero_not_an_exception(self):
        # int(float("inf")) raises OverflowError, which _to_int did not catch.
        for bad in ("inf", "-inf", "nan", "1e400"):
            with self.subTest(value=bad):
                self.assertEqual(n._to_int(bad), 0)

    def test_garbage_and_negative_counts_are_zero(self):
        for bad in ("", "  ", "abc", "-5", None):
            with self.subTest(value=bad):
                self.assertEqual(n._to_int(bad), 0)

    def test_float_formatted_integers_are_accepted(self):
        self.assertEqual(n._to_int("12.0"), 12)
        self.assertEqual(n._to_int("12.7"), 12)

    def test_empty_and_header_only_files_yield_nothing(self):
        self.assertEqual(parse(""), ([], []))
        self.assertEqual(parse("Date,UserId,auto_messages\n"), ([], []))

    def test_short_row_does_not_crash(self):
        """A truncated row leaves DictReader restval=None; csv.writer renders
        that as an empty field rather than the string 'None'."""
        facts, _ = parse("Date,UserId,Total_Messages\n2026-07-01,u1\n")
        self.assertEqual(len(facts), 1)
        self.assertNotIn("None", [str(v) for v in facts[0]])


class TestBookkeeping(unittest.TestCase):
    """The dedup views rank on these columns, so their semantics are load-bearing."""

    def test_part_name_is_content_addressed(self):
        same = n._part_name(KEY, "etag-a")
        self.assertEqual(same, n._part_name(KEY, "etag-a"))          # idempotent
        self.assertNotEqual(same, n._part_name(KEY, "etag-b"))        # self-healing
        self.assertNotEqual(same, n._part_name(KEY + "x", "etag-a"))  # key-sensitive

    def test_export_ts_prefers_the_filename_stamp(self):
        self.assertEqual(n._export_ts(KEY), "202607010000")

    def test_export_ts_falls_back_to_the_path(self):
        no_stamp = "user_report/us-east-1/2026/07/01/00/report.csv"
        self.assertEqual(n._export_ts(no_stamp), "202607010000")

    def test_export_date_is_a_projectable_partition_value(self):
        """Athena uses partition projection over export_date, so an unparseable
        value is not merely unfiltered - it is unreadable."""
        self.assertEqual(n._export_date(KEY), "2026-07-01")
        self.assertEqual(n._export_date("no/date/here/report.csv"), "unknown")

    def test_bookkeeping_columns_are_on_every_row(self):
        facts, models = parse("Date,UserId,auto_messages\n2026-07-01,u1,3\n")
        for col in ("src_path", "export_ts", "part_id", "processed_ts"):
            self.assertIn(col, n._FACT_OUT_HEADER)
            self.assertIn(col, n._MODEL_OUT_HEADER)
        self.assertEqual(fact(facts[0])["part_id"], "part-abc")
        h = n._MODEL_OUT_HEADER
        self.assertEqual(models[0][h.index("part_id")], "part-abc")

    def test_output_header_matches_the_external_table_ddl(self):
        """The parts are read positionally by OpenCSVSerDe, so the Lambda's
        column ORDER must match build_views.py's DDL exactly. A mismatch shifts
        every column silently."""
        bv = load("build_views")
        ddl = "\n".join(bv.report_tables_ddl("db", "bucket", "normalized"))
        for table, header in (("report_facts_raw", n._FACT_OUT_HEADER),
                              ("report_models_raw", n._MODEL_OUT_HEADER)):
            block = ddl[ddl.index(f"{table} ("):]
            block = block[:block.index("PARTITIONED BY")]
            cols = [ln.strip().split()[0] for ln in block.splitlines()
                    if ln.strip() and not ln.strip().startswith(("(", ")"))]
            cols = [c for c in cols if c not in (f"{table}", "CREATE")]
            with self.subTest(table=table):
                self.assertEqual(cols, header)


if __name__ == "__main__":
    unittest.main()
