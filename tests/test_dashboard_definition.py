"""Dashboard-definition invariants.

QuickSight accepts a Definition and silently IGNORES config it does not
understand - a sort spec pointed at the wrong field, a reference to a dataset
that no longer exists. Nothing errors; the visual just quietly behaves
differently. These tests assert the structural properties that would otherwise
only surface as a customer noticing a wrong number.
"""
from __future__ import annotations

import json
import re
import unittest

from _helpers import load

cd = load("create_dashboard")

ACCOUNT, REGION, PREFIX = "123456789012", "us-east-1", "kiro-test"


def definition(identity_mapping: bool = False) -> dict:
    return cd.build_definition(ACCOUNT, REGION, PREFIX, identity_mapping=identity_mapping)


def visuals(d):
    for sheet in d["Sheets"]:
        for v in sheet["Visuals"]:
            kind = next(iter(v))
            yield sheet["SheetId"], kind, v[kind]


class TestStructure(unittest.TestCase):

    def test_sheets_are_the_four_expected_ones_in_order(self):
        d = definition()
        self.assertEqual([s["SheetId"] for s in d["Sheets"]],
                         ["activity", "economics", "people", "user-detail"])

    def test_every_visual_id_is_unique(self):
        for mapping in (False, True):
            ids = [cfg["VisualId"] for _, _, cfg in visuals(definition(mapping))]
            with self.subTest(identity_mapping=mapping):
                self.assertEqual(len(ids), len(set(ids)), "duplicate VisualId")

    def test_every_visual_referenced_in_a_layout_exists(self):
        """A layout entry naming a removed visual leaves a blank tile."""
        for mapping in (False, True):
            d = definition(mapping)
            ids = {cfg["VisualId"] for _, _, cfg in visuals(d)}
            for sheet in d["Sheets"]:
                for layout in sheet.get("Layouts", []):
                    for el in layout["Configuration"]["GridLayout"]["Elements"]:
                        if el.get("ElementType") == "VISUAL":
                            with self.subTest(sheet=sheet["SheetId"], el=el["ElementId"]):
                                self.assertIn(el["ElementId"], ids)

    def test_every_visual_has_a_layout_entry(self):
        """A visual with no layout entry does not render at all."""
        for mapping in (False, True):
            d = definition(mapping)
            placed = {el["ElementId"]
                      for sheet in d["Sheets"]
                      for layout in sheet.get("Layouts", [])
                      for el in layout["Configuration"]["GridLayout"]["Elements"]
                      if el.get("ElementType") == "VISUAL"}
            for sheet_id, _, cfg in visuals(d):
                with self.subTest(identity_mapping=mapping, visual=cfg["VisualId"]):
                    self.assertIn(cfg["VisualId"], placed)

    def test_every_visual_has_a_subtitle(self):
        """Subtitles carry the caveats (fixed windows, lifetime-not-filtered).
        A visual without one is a number with no context."""
        for _, _, cfg in visuals(definition()):
            with self.subTest(visual=cfg["VisualId"]):
                self.assertIn("Subtitle", cfg)


class TestDatasetReferences(unittest.TestCase):

    def test_every_referenced_identifier_is_declared(self):
        """An undeclared identifier makes QuickSight drop the field silently."""
        for mapping in (False, True):
            d = definition(mapping)
            declared = {x["Identifier"] for x in d["DataSetIdentifierDeclarations"]}
            used = set(re.findall(r'"DataSetIdentifier":\s*"([^"]+)"', json.dumps(d)))
            with self.subTest(identity_mapping=mapping):
                self.assertLessEqual(used, declared,
                                     f"undeclared: {sorted(used - declared)}")

    def test_no_declared_identifier_is_unused(self):
        for mapping in (False, True):
            d = definition(mapping)
            blob = json.dumps(d)
            unused = [x["Identifier"] for x in d["DataSetIdentifierDeclarations"]
                      if f'"DataSetIdentifier": "{x["Identifier"]}"' not in blob]
            with self.subTest(identity_mapping=mapping):
                self.assertEqual(unused, [], f"declared but unused: {unused}")


class TestTierFilterConsistency(unittest.TestCase):
    """A visual must be FILTERED on the same column it GROUPS by. When the
    All-users table grouped by the per-user `user_tier` while the picker filtered
    the per-row `subscription_tier`, selecting Tier=Pro returned rows whose tier
    column read 'Pro+' - which reads as a data-integrity bug."""

    def _tier_filters(self, d):
        out = {}
        for fg in d["FilterGroups"]:
            f = fg["Filters"][0].get("CategoryFilter")
            if f and "tier" in fg["FilterGroupId"]:
                out[fg["FilterGroupId"]] = (f["Column"]["DataSetIdentifier"],
                                            f["Column"]["ColumnName"])
        return out

    def test_base_backed_tier_filters_use_user_tier(self):
        for mapping in (False, True):
            for gid, (ds, col) in self._tier_filters(definition(mapping)).items():
                if ds == "base":
                    with self.subTest(identity_mapping=mapping, group=gid):
                        self.assertEqual(
                            col, "user_tier",
                            f"{gid} filters base on {col}; base-backed visuals "
                            f"group by user_tier, so the two must agree",
                        )

    def test_economics_visuals_group_by_user_tier(self):
        """Grouping Economics by the per-row tier counted a mid-window upgrader
        in BOTH tiers - the donut's slices summed to more than its own total."""
        d = definition()
        econ = [cfg for sid, _, cfg in visuals(d) if sid == "economics"]
        self.assertTrue(econ)
        for cfg in econ:
            blob = json.dumps(cfg)
            if '"subscription_tier"' in blob:
                with self.subTest(visual=cfg["VisualId"]):
                    self.fail(f"{cfg['VisualId']} groups by the per-row "
                              f"subscription_tier; use user_tier")


class TestDrillThrough(unittest.TestCase):
    """The People -> User-detail drill sets DrillUser from a positional FieldId.
    QuickSight validates that the field EXISTS, not what it means, so inserting a
    dimension before user_label would silently retarget the drill."""

    def test_drill_source_field_is_the_user_label_column(self):
        for mapping in (False, True):
            d = definition(mapping)
            table = next(cfg for _, kind, cfg in visuals(d)
                         if cfg["VisualId"] == "p-all-users")
            dims = table["ChartConfiguration"]["FieldWells"]["TableAggregatedFieldWells"]["GroupBy"]
            first = dims[0]["CategoricalDimensionField"]
            with self.subTest(identity_mapping=mapping):
                self.assertEqual(first["Column"]["ColumnName"], "user_label",
                                 "the drill Action targets d0; user_label must "
                                 "stay the FIRST dimension")
                self.assertTrue(first["FieldId"].endswith("-d0"))

    def test_a_drill_action_exists_on_the_all_users_table(self):
        d = definition()
        table = next(cfg for _, _, cfg in visuals(d) if cfg["VisualId"] == "p-all-users")
        actions = json.dumps(table.get("Actions", []))
        self.assertIn("DrillUser", actions, "no DrillUser set-parameter action")


class TestUserDetailMultiSelect(unittest.TestCase):
    """User detail must accept a LIST of users. A customer needed usage for a
    specific set of employees and the sheet allowed one at a time.

    Multi-select alone was not enough: the profile strip is lifetime and the KPI
    tiles are group totals, so neither answers "what did each of these people use
    in this window?". The per-user period table is what makes the selection
    useful, so it is pinned here alongside the parameter."""

    def _param(self, d, name):
        for p in d["ParameterDeclarations"]:
            decl = p.get("StringParameterDeclaration")
            if decl and decl["Name"] == name:
                return decl
        self.fail(f"no string parameter {name}")

    def _sheet(self, d, sheet_id):
        return next(s for s in d["Sheets"] if s["SheetId"] == sheet_id)

    def test_drill_user_is_multi_valued(self):
        self.assertEqual(self._param(definition(), "DrillUser")["ParameterValueType"],
                         "MULTI_VALUED")

    def test_user_picker_is_a_multi_select_dropdown(self):
        """A MULTI_VALUED parameter behind a SINGLE_SELECT control can still only
        ever receive one value from the UI - the control is what the user sees."""
        controls = self._sheet(definition(), "user-detail")["ParameterControls"]
        picker = next(c["Dropdown"] for c in controls
                      if c.get("Dropdown", {}).get("SourceParameterName") == "DrillUser")
        self.assertEqual(picker["Type"], "MULTI_SELECT")
        self.assertEqual(picker["DisplayOptions"]["SelectAllOptions"]["Visibility"], "VISIBLE")

    def test_no_calculated_field_uses_drill_user_as_a_scalar(self):
        """A multi-valued parameter has no single value. A calc field comparing
        against ${DrillUser} would silently misbehave once it became a list."""
        for mapping in (False, True):
            for cf in definition(mapping)["CalculatedFields"]:
                with self.subTest(identity_mapping=mapping, field=cf["Name"]):
                    self.assertNotIn("${DrillUser}", cf["Expression"])

    def test_every_user_detail_dataset_is_filtered_by_the_selection(self):
        """If one dataset escapes the drill filter, its visuals show EVERYONE
        while the rest of the sheet shows the selection - a silent mismatch."""
        d = definition()
        visual_datasets = set(re.findall(r'"DataSetIdentifier":\s*"([^"]+)"',
                                         json.dumps(self._sheet(d, "user-detail")["Visuals"])))
        drilled = set()
        for fg in d["FilterGroups"]:
            f = fg["Filters"][0].get("CategoryFilter", {})
            cfg = f.get("Configuration", {}).get("CustomFilterConfiguration", {})
            if cfg.get("ParameterName") == "DrillUser":
                scopes = fg["ScopeConfiguration"]["SelectedSheets"]["SheetVisualScopingConfigurations"]
                if any(s["SheetId"] == "user-detail" and s["Scope"] == "ALL_VISUALS" for s in scopes):
                    self.assertEqual(cfg["MatchOperator"], "EQUALS")
                    drilled.add(f["Column"]["DataSetIdentifier"])
        self.assertLessEqual(visual_datasets, drilled,
                             f"User-detail datasets not filtered by the user "
                             f"selection: {sorted(visual_datasets - drilled)}")

    def test_per_user_period_table_exists_and_is_windowed(self):
        """The visual that turns a multi-selection into per-person numbers. It
        must read `base` (date-filtered on this sheet), not the lifetime
        `users` dataset, or it would repeat the profile strip."""
        for mapping in (False, True):
            d = definition(mapping)
            table = next((cfg for sid, _, cfg in visuals(d)
                          if sid == "user-detail" and cfg["VisualId"] == "u-period-users"), None)
            with self.subTest(identity_mapping=mapping):
                self.assertIsNotNone(table, "u-period-users missing from User detail")
                wells = table["ChartConfiguration"]["FieldWells"]["TableAggregatedFieldWells"]
                first = wells["GroupBy"][0]["CategoricalDimensionField"]["Column"]
                self.assertEqual(first, {"DataSetIdentifier": "base", "ColumnName": "user_label"})
                date_fg = next(fg for fg in d["FilterGroups"] if fg["FilterGroupId"] == "fg-date-base")
                scopes = date_fg["ScopeConfiguration"]["SelectedSheets"]["SheetVisualScopingConfigurations"]
                self.assertTrue(any(s["SheetId"] == "user-detail" and s["Scope"] == "ALL_VISUALS"
                                    for s in scopes),
                                "base is not date-filtered on User detail")

    def test_per_user_table_agrees_with_the_people_table(self):
        """Same dataset, same columns, same aggregations as p-all-users, so a
        user's numbers match to the cent on both sheets. Divergence here is how
        two tables end up disagreeing and a customer stops trusting both."""
        def wells(vid):
            cfg = next(c for _, _, c in visuals(definition(True)) if c["VisualId"] == vid)
            w = cfg["ChartConfiguration"]["FieldWells"]["TableAggregatedFieldWells"]
            field = lambda x: next(iter(x.values()))  # unwrap {"<Kind>Field": {...}}
            dims = [field(x)["Column"]["ColumnName"] for x in w["GroupBy"]]
            vals = [(field(x)["Column"]["ColumnName"],
                     json.dumps(field(x).get("AggregationFunction"), sort_keys=True))
                    for x in w["Values"]]
            return dims, vals
        self.assertEqual(wells("u-period-users"), wells("p-all-users"))

    def test_people_click_through_still_targets_the_picker(self):
        """Clicking a row on People must still land on that one user."""
        d = definition()
        table = next(cfg for _, _, cfg in visuals(d) if cfg["VisualId"] == "p-all-users")
        ops = [op for a in table["Actions"] for op in a["ActionOperations"]]
        dests = [c["DestinationParameterName"]
                 for op in ops if "SetParametersOperation" in op
                 for c in op["SetParametersOperation"]["ParameterValueConfigurations"]]
        self.assertEqual(dests, ["DrillUser"])


class TestAssetName(unittest.TestCase):
    """Parallel deployments must be distinguishable in the QuickSight console.

    The asset ID was namespaced per deployment but the display NAME was a fixed
    constant, so four deployments in one account showed as four identical rows
    titled "Kiro User Analytics" - you had to open each one to tell which was
    which. STACK_PREFIX is also the CloudFormation stack name, so deriving from
    it cannot collide.
    """

    def test_distinct_prefixes_give_distinct_names(self):
        prefixes = ["kiro-analytics", "kiro-euc1", "kiro-synthetic", "kiro-xregion"]
        names = [cd.asset_name(p) for p in prefixes]
        self.assertEqual(len(set(names)), len(prefixes),
                         f"duplicate console names: {names}")

    def test_default_deployment_keeps_the_plain_name(self):
        """'Kiro User Analytics (kiro-analytics)' is noise when there is one."""
        self.assertEqual(cd.asset_name(cd.DEFAULT_RESOURCE_PREFIX), cd.DEFAULT_NAME)

    def test_non_default_prefix_is_visible_in_the_name(self):
        name = cd.asset_name("kiro-euc1")
        self.assertIn("kiro-euc1", name)
        self.assertIn(cd.DEFAULT_NAME, name)

    def test_explicit_override_wins(self):
        self.assertEqual(cd.asset_name("kiro-euc1", "Kiro - EU Prod"), "Kiro - EU Prod")

    def test_name_respects_the_api_length_cap(self):
        """QuickSight caps Name at 2048 chars; a long prefix must not make the
        call fail after the datasets already exist."""
        for prefix, override in (("x" * 4000, None), ("p", "y" * 4000)):
            with self.subTest(prefix=prefix[:12], override=bool(override)):
                self.assertLessEqual(len(cd.asset_name(prefix, override)), 2048)

    def test_upsert_sends_the_name_on_both_create_and_update(self):
        """Update must carry it too, or an existing deployment would keep its old
        duplicate name forever."""
        src = (cd.__file__ and open(cd.__file__).read()) or ""
        self.assertEqual(src.count('"Name": name,'), 3,
                         "expected Name wired into create + both update paths")


class TestIdentityColumns(unittest.TestCase):

    def test_identity_columns_appear_only_when_mapping_is_on(self):
        """With mapping off they would be empty columns on every row."""
        off, on = json.dumps(definition(False)), json.dumps(definition(True))
        for col in ("idc_username", "idc_email"):
            with self.subTest(column=col):
                self.assertNotIn(f'"{col}"', off)
                self.assertIn(f'"{col}"', on)

    def test_the_export_pivot_carries_a_join_key(self):
        """user_label is a display name and is absent from Kiro's own
        subscriptions export, so an export of this grid needs a joinable key."""
        d = definition(identity_mapping=True)
        pivot = next(cfg for _, _, cfg in visuals(d) if cfg["VisualId"] == "p-user-daily")
        rows = pivot["ChartConfiguration"]["FieldWells"]["PivotTableAggregatedFieldWells"]["Rows"]
        cols = [r["CategoricalDimensionField"]["Column"]["ColumnName"] for r in rows]
        self.assertEqual(cols[0], "user_label")
        self.assertIn("idc_username", cols)


if __name__ == "__main__":
    unittest.main()
