"""Tier-label rendering.

A customer reported credits split across multiple rows for two users. Root cause:
four views each hand-rolled a `CASE upper(<expr>) ... ELSE <A DIFFERENT expr>`.
Because PRO_MAX was missing from the WHEN list it fell through to the ELSE - and
in the `user_tier` case the CASE tested the per-USER windowed tier while the ELSE
returned the per-ROW tier, so the column stopped being constant per user and the
All-users table split one user into several partial rows.

The fix renders one shared expression, substituting the raw operand at EVERY
position including the fallback. These tests pin that property, since a
regression here reproduces a bug that reached a customer.
"""
from __future__ import annotations

import re
import unittest

from _helpers import ATHENA, load

bv = load("build_views")


class TestTierLabelExpr(unittest.TestCase):

    def test_operand_appears_in_the_fallback_too(self):
        """The whole bug: ELSE must reference the SAME expression the CASE tests."""
        raw = "max_by(t, d) OVER (PARTITION BY u)"
        expr = bv.tier_label_expr(raw)
        head, _, fallback = expr.partition("ELSE")
        self.assertIn(raw, head, "CASE operand missing")
        self.assertIn(raw, fallback, "fallback does not reference the same operand")

    def test_no_other_column_leaks_into_the_fallback(self):
        """Guards the exact original defect: a *different* column in the ELSE."""
        expr = bv.tier_label_expr("windowed_expr")
        fallback = expr.split("ELSE", 1)[1]
        self.assertNotIn("subscription_tier", fallback)
        self.assertNotIn("subscription_tier_raw", fallback)

    def test_all_four_kiro_tiers_are_mapped(self):
        """Kiro has exactly four paid tiers. PRO_MAX was the one originally
        missing, which is what triggered the fallback."""
        expr = bv.tier_label_expr("t")
        for code, label in (("PRO", "Pro"), ("PRO_PLUS", "Pro+"),
                            ("PRO_MAX", "Pro Max"), ("POWER", "Power")):
            with self.subTest(code=code):
                self.assertIn(f"WHEN '{code}' THEN '{label}'", expr)

    def test_no_free_tier(self):
        """Free is Builder-ID only; an enterprise export cannot contain it, so a
        Free entry would be misleading rather than defensive."""
        self.assertNotIn("'Free'", bv.tier_label_expr("t"))

    def test_blank_maps_to_unknown_before_the_fallback(self):
        expr = bv.tier_label_expr("t")
        self.assertLess(expr.index("WHEN '' THEN 'Unknown'"), expr.index("ELSE"))

    def test_unknown_tier_is_title_cased_not_raw(self):
        """A tier Kiro invents later must render sensibly on a dashboard nobody
        has redeployed, rather than leaking SCREAMING_SNAKE."""
        fallback = bv.tier_label_expr("t").split("ELSE", 1)[1]
        self.assertIn("array_join", fallback)
        self.assertIn("upper(substr(w, 1, 1))", fallback)
        self.assertIn("lower(substr(w, 2))", fallback)

    def test_operand_is_upper_and_null_safe(self):
        expr = bv.tier_label_expr("t")
        self.assertIn("upper(COALESCE(t, ''))", expr)

    def test_colour_map_covers_every_rendered_label(self):
        """An unmapped label still renders, but from the auto palette - which can
        collide with a hue reserved for another tier."""
        cd = load("create_dashboard")
        for label in bv._TIER_LABELS.values():
            with self.subTest(label=label):
                self.assertIn(label, cd._TIER_COLORS)
        self.assertIn("Unknown", cd._TIER_COLORS)


class TestTierUsageInViews(unittest.TestCase):
    """The rendered views must use the shared expression, not a hand-rolled CASE."""

    def test_no_view_hand_rolls_a_tier_case(self):
        for sql_file in sorted(ATHENA.glob("*.sql")):
            body = sql_file.read_text()
            with self.subTest(view=sql_file.name):
                self.assertNotRegex(
                    body, r"WHEN\s+'PRO_PLUS'\s+THEN",
                    f"{sql_file.name} hand-rolls a tier CASE; use ${{tier_label_*}}",
                )

    def test_max_by_orders_by_a_date_not_a_string(self):
        """`date` is a varchar in report_facts. Ordering lexically picks
        '9/1/2026' over '10/1/2026' under a non-ISO export format, resurrecting
        the 'upgrade to Power still shows Pro+' bug the comments call fixed."""
        for sql_file in sorted(ATHENA.glob("*.sql")):
            body = sql_file.read_text()
            for m in re.finditer(r"max_by\(([^)]*?),\s*([^),]+)\)", body):
                order_key = m.group(2).strip()
                with self.subTest(view=sql_file.name, order_by=order_key):
                    self.assertNotEqual(
                        order_key, "date",
                        f"{sql_file.name}: max_by orders by the raw varchar `date`",
                    )


if __name__ == "__main__":
    unittest.main()
