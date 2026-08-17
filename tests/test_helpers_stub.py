"""The boto3/botocore stub must cover every name the scripts import.

The suite is deliberately dependency-free: no boto3 needed (see the #28 fix,
where the suite silently relied on boto3 arriving transitively via cfn-lint and
so passed in the lint job while failing in a bare Python job). `_stub_aws()`
makes the scripts importable by faking the AWS modules they import at module
scope.

That stub is hand-maintained, which means it drifts. `scripts/check_pricing.py`
was added importing `BotoCoreError`, which the stub did not define - so loading
it raised

    ImportError: cannot import name 'BotoCoreError' from 'botocore.exceptions'

an error that points at botocore rather than at the stub, which is a confusing
place to begin debugging. Nothing imported that module, so no test failed; the
gap was invisible.

This file removes the "nothing imports it yet" caveat by deriving the required
names from what the scripts ACTUALLY import, rather than from a list someone has
to remember to update. A new `from botocore... import Whatever` in any script
fails here, at the stub, with a message naming the missing symbol.
"""
from __future__ import annotations

import re
import unittest

from _helpers import SCRIPTS, _stub_aws


def imported_names() -> dict[str, set[str]]:
    """module -> {names} for every `from <aws module> import ...` in scripts/."""
    wanted: dict[str, set[str]] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text()
        for module, names in re.findall(
                r"^from\s+((?:boto3|botocore)[\w.]*)\s+import\s+([^\n(]+)", text, re.M):
            wanted.setdefault(module, set()).update(
                n.strip().split(" as ")[0] for n in names.split(",") if n.strip())
    return wanted


class TestStubCoversWhatScriptsImport(unittest.TestCase):

    def test_every_from_import_resolves_against_the_stub(self):
        _stub_aws()
        wanted = imported_names()
        self.assertTrue(wanted, "parsed no AWS imports from scripts/ - check the regex")
        for module, names in sorted(wanted.items()):
            mod = __import__(module, fromlist=list(names))
            for name in sorted(names):
                with self.subTest(statement=f"from {module} import {name}"):
                    self.assertTrue(
                        hasattr(mod, name),
                        f"scripts/ imports {name} from {module}, but the offline "
                        f"stub in tests/_helpers.py does not define it. Add it to "
                        f"_stub_aws() - otherwise importing that script fails with "
                        f"an ImportError that appears to come from botocore.",
                    )

    def test_plain_import_boto3_works(self):
        _stub_aws()
        import boto3
        self.assertTrue(hasattr(boto3, "client"))


class TestStubFidelity(unittest.TestCase):
    """Where the stub models real AWS behaviour, it must model it correctly - a
    stub that is merely importable can let a test pass against a hierarchy that
    does not exist."""

    def test_client_error_and_botocore_error_are_siblings(self):
        """Real botocore: issubclass(ClientError, BotoCoreError) is False. The
        scripts catch them as a TUPLE, so collapsing them into one class (or
        making one inherit the other) would make a test that exercises only one
        branch look like it covered both."""
        _stub_aws()
        from botocore.exceptions import BotoCoreError, ClientError
        self.assertFalse(issubclass(ClientError, BotoCoreError))
        self.assertFalse(issubclass(BotoCoreError, ClientError))
        for exc in (BotoCoreError, ClientError):
            with self.subTest(exception=exc.__name__):
                self.assertTrue(issubclass(exc, Exception))

    def test_client_error_keeps_the_botocore_constructor_shape(self):
        """`except ClientError as e: e.response["Error"]["Code"]` is the idiom the
        scripts use, so the stub must carry .response."""
        _stub_aws()
        from botocore.exceptions import ClientError
        err = ClientError({"Error": {"Code": "AccessDenied"}}, "GetProducts")
        self.assertEqual(err.response["Error"]["Code"], "AccessDenied")
        self.assertEqual(err.operation_name, "GetProducts")

    def test_the_stub_refuses_to_create_an_aws_client(self):
        """When the stub IS in force, creating a client must fail loudly rather
        than return something inert that a test might use by accident.

        Asserted against the stub's own classes rather than `import boto3`,
        because `_stub_aws()` deliberately does nothing when real boto3 is
        installed - so on a developer machine (or the lint CI job, which pulls
        boto3 in via cfn-lint) `boto3.client` is the genuine function and
        naturally does not raise. An earlier version of this test asserted on the
        imported module and so failed in exactly the environment most
        contributors have."""
        _stub_aws()
        import boto3
        stubbed = getattr(boto3, "__file__", None) is None
        if not stubbed:
            self.skipTest("real boto3 is installed, so _stub_aws() is inert here "
                          "by design; the blocked-boto3 run covers this")
        for factory in ("client", "resource", "Session"):
            with self.subTest(factory=factory):
                with self.assertRaises(AssertionError):
                    getattr(boto3, factory)("s3")


if __name__ == "__main__":
    unittest.main()
