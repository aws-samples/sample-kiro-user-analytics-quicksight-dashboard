"""The cost section's arithmetic must be internally consistent.

A wrong number in a cost table is worse than no cost table: someone budgets from
it. The figures were measured against live deployments and verified against the
AWS Price List API, but nothing stops a later edit from changing one cell and
leaving the rest - so the licence table is recomputed here from the unit prices
the section itself states.

KNOWN LIMIT of everything in this file: it only reads the README, so it catches
internal INCONSISTENCY and never STALENESS. If AWS reprices a reader tomorrow,
every test here still passes while the whole Cost section is wrong. Nothing
offline can detect that, so the mitigations are elsewhere and deliberate: the
figures are labelled us-east-1 list price, the authoritative pages are linked,
and `scripts/check_pricing.py` diffs the documented prices against the live
Price List API at each release (it needs credentials, so it cannot run in CI -
and failing a contributor's PR because AWS changed a price would be noise).
The tests at the bottom of this file at least keep that script and the README
from drifting apart.
"""
from __future__ import annotations

import re
import unittest

from _helpers import REPO, SCRIPTS

README = (REPO / "README.md").read_text()
CHECKER = (SCRIPTS / "check_pricing.py").read_text()


def cost_section() -> str:
    body = README.split("\n## Cost\n", 1)
    assert len(body) == 2, "README has no '## Cost' section"
    return body[1].split("\n## ", 1)[0]


SECTION = cost_section()


def money(text: str) -> list[int]:
    """Whole-dollar amounts, ignoring cents figures and prices-per-TB."""
    return [int(m.replace(",", ""))
            for m in re.findall(r"\$([0-9][0-9,]*)(?![0-9.]*\d\s*/\s*TB)(?!\.\d)", text)]


class TestLicenceArithmetic(unittest.TestCase):
    """Recompute the licence table from the unit prices the section states."""

    AUTHOR, READER, FLAT = 24, 3, 250

    def test_stated_unit_prices_are_present(self):
        """If a unit price is edited, the expectations below must be revisited -
        so assert the prices this test assumes actually appear in the text."""
        for price in (self.AUTHOR, self.READER, self.FLAT):
            with self.subTest(price=price):
                self.assertIn(f"${price}", SECTION)

    def test_licence_table_totals_are_correct(self):
        table = SECTION.split("### QuickSight licences", 1)[1].split("###", 1)[0]
        rows = re.findall(r"\|\s*1 author(?:,| \+) ([\d,]+|no) readers?[^|]*\|\s*\*{0,2}\$([\d,]+)",
                          table)
        self.assertGreaterEqual(len(rows), 4, f"could not parse licence rows: {table[:400]}")
        for readers_txt, total_txt in rows:
            readers = 0 if readers_txt == "no" else int(readers_txt.replace(",", ""))
            expected = self.AUTHOR + readers * self.READER
            with self.subTest(readers=readers):
                self.assertEqual(int(total_txt.replace(",", "")), expected,
                                 f"{readers} readers should be ${expected}")

    def test_the_multiplier_claim_matches_the_table(self):
        """The headline trap: entitling every seat instead of ~25 people."""
        m = re.search(r"\*\*(\d+)× bill increase\*\*", SECTION)
        self.assertIsNotNone(m, "no 'N× bill increase' claim found")
        small = self.AUTHOR + 25 * self.READER
        big = self.AUTHOR + 8000 * self.READER
        self.assertEqual(int(m.group(1)), round(big / small))

    def test_both_endpoints_of_the_multiplier_are_shown(self):
        """A ratio with no absolute numbers is unactionable."""
        for amount in ("$99", "$24,024"):
            with self.subTest(amount=amount):
                self.assertIn(amount, SECTION)


class TestPipelineFraming(unittest.TestCase):

    def test_licences_are_named_as_the_dominant_cost(self):
        """The one thing a reader must take away. Pipeline cost is a rounding
        error; budgeting from it is the mistake this section exists to prevent."""
        opening = SECTION.split("###", 1)[0].lower()
        self.assertIn("licence", opening)
        self.assertIn("reader", opening)

    def test_the_flat_fee_is_documented_with_its_trigger(self):
        """$250/month applies account-wide and is ~6000x the pipeline, so it
        must be attributable - otherwise it gets blamed on this sample."""
        self.assertIn("$250", SECTION)
        self.assertRegex(SECTION, r"(?i)pro user")
        self.assertRegex(SECTION, r"(?i)amazon q")

    def test_a_pro_user_detection_command_is_given(self):
        """Naming a charge without a way to check for it is not actionable."""
        self.assertIn("list-users", SECTION)
        self.assertIn("PRO", SECTION)

    def test_retained_kms_key_cost_is_disclosed(self):
        """A retained CMK bills $1/month forever - 25x this pipeline - and
        teardown deliberately leaves it behind."""
        self.assertRegex(SECTION, r"\$1/month")


class TestReducingCost(unittest.TestCase):

    def test_reader_count_is_the_first_recommendation(self):
        """Ordering is the advice. Anything above 'entitle fewer readers'
        would be optimising a rounding error."""
        steps = SECTION.split("### Reducing cost", 1)[1]
        first = re.search(r"^1\.\s+\*\*(.+?)\*\*", steps, re.M)
        self.assertIsNotNone(first, "no numbered recommendations found")
        self.assertRegex(first.group(1), r"(?i)reader")


class TestCostSectionIsLinked(unittest.TestCase):

    def test_cost_section_has_a_heading_anchor_target(self):
        self.assertIn("## Cost", README)

    def test_pricing_pages_are_referenced(self):
        """List prices go stale; the authoritative source must be one click
        away and the figures must be labelled as list price."""
        self.assertIn("aws.amazon.com/quicksight/pricing", SECTION)
        self.assertRegex(SECTION, r"(?i)list price")

    def test_staleness_is_disclosed_with_a_way_to_recheck(self):
        """The honest hedge for the limit described in this module's docstring:
        a reader must be told the numbers can age, and handed the command that
        re-verifies them."""
        self.assertRegex(SECTION, r"(?i)stale")
        self.assertIn("scripts/check_pricing.py", SECTION)


class TestPricingCheckerStaysInSyncWithTheDocs(unittest.TestCase):
    """The offline tests cannot see AWS repricing; check_pricing.py can. That
    only helps if the two agree on the numbers, so pin them together - otherwise
    the release check silently validates prices the README no longer states."""

    def _expected(self) -> dict[str, float]:
        block = CHECKER.split("EXPECTED = {", 1)[1].split("}", 1)[0]
        return {label: float(price) for label, price in
                re.findall(r'\("([^"]+)",\s*([\d.]+)\)', block)}

    def test_checker_declares_the_prices_the_readme_uses(self):
        expected = self._expected()
        self.assertTrue(expected, "could not parse EXPECTED from check_pricing.py")
        for name, price in (("Reader", 3.0), ("fee", 250.0)):
            match = [v for k, v in expected.items() if name.lower() in k.lower()]
            with self.subTest(price=name):
                self.assertIn(price, match,
                              f"check_pricing.py does not expect ${price} for {name}; "
                              f"it and the README's Cost section have drifted")

    def test_checker_is_not_wired_into_offline_checks(self):
        """It needs credentials and network. In run-checks.sh it would fail for
        every contributor without AWS access, and in CI it would fail fork PRs -
        so the release process calls it, not the test suite."""
        self.assertNotIn("check_pricing", (SCRIPTS / "run-checks.sh").read_text())

    def test_the_release_process_calls_it(self):
        """A check nobody runs is not a mitigation."""
        contributing = (REPO / "CONTRIBUTING.md").read_text()
        self.assertIn("scripts/check_pricing.py", contributing)

    def test_unverifiable_figures_are_declared_rather_than_skipped(self):
        """The Price List API publishes no SKU for the plain Author price, so
        the script must SAY so instead of quietly checking a subset and
        implying the whole section was validated."""
        self.assertIn("NOT_IN_API", CHECKER)
        self.assertRegex(CHECKER, r"(?i)author")


if __name__ == "__main__":
    unittest.main()
