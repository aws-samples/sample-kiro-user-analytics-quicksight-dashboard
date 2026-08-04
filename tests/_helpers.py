"""Shared test helpers: import the scripts without needing a package install."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
ATHENA = REPO / "athena"
CFN = REPO / "cfn"


def _stub_aws() -> None:
    """Make the scripts importable without boto3 installed.

    The scripts create AWS clients at CALL time, but they import boto3 and
    botocore at module scope and one builds a botocore Config constant there, so
    the import must resolve even though no test touches AWS. Only stubs what is
    genuinely absent, so a machine or CI job that HAS boto3 exercises the real
    library rather than this shim.

    Without this the suite silently depended on boto3 arriving transitively (via
    cfn-lint), so it passed in the lint job and failed in a bare Python job.
    """
    # Already real-imported, or already stubbed by an earlier load(). Checking
    # sys.modules FIRST matters: find_spec() raises ValueError (not ImportError)
    # for a module present in sys.modules with __spec__ None, which is exactly
    # what these stubs are - so calling it twice would explode on the 2nd load.
    if "boto3" in sys.modules:
        return
    try:
        if importlib.util.find_spec("boto3") is not None:
            return
    except (ImportError, ValueError):
        pass

    def _no_aws(*_args, **_kwargs):
        raise AssertionError(
            "the offline test suite must never create an AWS client "
            "(boto3 is stubbed; if a test needs this, mock it explicitly)"
        )

    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = _no_aws
    boto3_mod.resource = _no_aws
    boto3_mod.Session = _no_aws

    class Config:
        """Accepts and records whatever kwargs the scripts pass."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ClientError(Exception):
        """Same constructor shape as botocore, so `except ClientError` works."""

        def __init__(self, error_response=None, operation_name=None):
            super().__init__(error_response, operation_name)
            self.response = error_response or {}
            self.operation_name = operation_name

    botocore_mod = types.ModuleType("botocore")
    config_mod = types.ModuleType("botocore.config")
    config_mod.Config = Config
    exceptions_mod = types.ModuleType("botocore.exceptions")
    exceptions_mod.ClientError = ClientError
    botocore_mod.config = config_mod
    botocore_mod.exceptions = exceptions_mod

    for name, mod in (
        ("boto3", boto3_mod),
        ("botocore", botocore_mod),
        ("botocore.config", config_mod),
        ("botocore.exceptions", exceptions_mod),
    ):
        sys.modules.setdefault(name, mod)


def load(module_name: str):
    """Import a module from scripts/ by filename stem.

    The scripts are standalone files, not an installed package - a Lambda zip
    contains one .py file - so tests load them by path rather than by import.
    """
    path = SCRIPTS / f"{module_name}.py"
    # Match on __file__, not just the name: a same-named installed module must
    # not be mistaken for ours.
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    _stub_aws()
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        # NEVER leave a half-executed module cached. module_from_spec registers
        # it before exec, so a failed import would otherwise be served to every
        # later load() as an attribute-less shell, turning one ImportError into a
        # cascade of confusing AttributeErrors in unrelated tests.
        sys.modules.pop(module_name, None)
        raise
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
