"""Shared test helpers: import the scripts without needing a package install."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
ATHENA = REPO / "athena"
CFN = REPO / "cfn"


def load(module_name: str):
    """Import a module from scripts/ by filename stem.

    The scripts are standalone files, not an installed package - a Lambda zip
    contains one .py file - so tests load them by path rather than by import.
    """
    path = SCRIPTS / f"{module_name}.py"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# The substitutions build_views.py makes at render time. Tests that render SQL
# use these so a new placeholder in a .sql file fails loudly here rather than at
# deploy time against a customer's Athena.
def view_substitutions(bv, *, identity_mapping: bool = False,
                       database: str = "test_db", email_expr: str = "email") -> dict:
    parts = bv.render_identity_label_parts(database, email_expr, identity_mapping)
    return dict(
        database=database,
        email_expr=email_expr,
        tier_label_row=bv.tier_label_expr("subscription_tier"),
        tier_label_user=bv.tier_label_expr(
            "max_by(COALESCE(subscription_tier, ''), TRY(CAST(date AS date)))"
            " OVER (PARTITION BY userid)"
        ),
        tier_label_dim=bv.tier_label_expr("subscription_tier_raw"),
        tier_label_model=bv.tier_label_expr("m.subscription_tier"),
        **parts,
    )
