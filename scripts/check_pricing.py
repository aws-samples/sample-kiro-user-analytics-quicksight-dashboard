#!/usr/bin/env python3
"""Check the README's cost figures against the live AWS Price List API.

Why this exists
---------------
`tests/test_cost_docs.py` recomputes the licence table from the unit prices the
README itself states, so a figure edited in one cell and left stale in another
fails CI. But that test only reads the README: it can catch internal
INCONSISTENCY, never STALENESS. If AWS reprices a QuickSight reader tomorrow,
every one of those tests still passes while the whole section is wrong.

This script closes that gap by comparing the README's stated prices against what
AWS currently charges. It is deliberately NOT part of `run-checks.sh` or CI:

  * it needs credentials and network, and CI is offline by design so fork PRs
    can run it;
  * AWS repricing is not a contributor's fault, so failing their PR for it would
    be noise.

Run it when cutting a release (see CONTRIBUTING.md "Versioning"):

    python3 scripts/check_pricing.py

Exit codes: 0 = README matches AWS, 1 = a price drifted, 2 = could not check.

Scope, honestly stated
----------------------
The Price List API does not publish every SKU. As of this writing it carries
Reader, Reader Pro, Author Pro, the per-account Q/Pro fee and SPICE capacity for
`AmazonQuickSight` - but there is NO record for the plain $24 Author, in any
Region. So this script verifies what the API actually exposes and explicitly
reports what it cannot, rather than quietly checking a subset and implying the
whole section was validated. The unverifiable figures still need a human glance
at https://aws.amazon.com/quicksight/pricing/ - which the script prints.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REPO = pathlib.Path(__file__).resolve().parent.parent
README = REPO / "README.md"

# The Price List API is only served from these two Regions regardless of which
# Region's prices you are asking about.
PRICING_API_REGION = "us-east-1"

# usagetype -> (what the README calls it, expected USD). us-east-1 list price.
# Keep in sync with the Cost section. A price present here but absent from the
# API is reported as UNVERIFIABLE rather than silently passing.
EXPECTED = {
    "USE1-Reader-Enterprise-Month":     ("QuickSight Reader (per user/month)",     3.00),
    "USE1-Reader-Pro-Enterprise-Month": ("QuickSight Reader Pro (per user/month)", 20.00),
    "USE1-Author-Pro-Enterprise-Month": ("QuickSight Author Pro (per user/month)", 40.00),
    "USE1-Amazon-Q-QS-Fee":             ("Flat per-account Q/Pro fee",             250.00),
    "USE1-QS-Enterprise-SPICE":         ("Extra SPICE capacity (per GB/month)",    0.38),
}

# Figures the README states that the API does NOT publish. Listed so the output
# is honest about coverage instead of implying a full check.
NOT_IN_API = {
    "QuickSight Author (per user/month)": 24.00,
}


def live_prices(region: str) -> dict[str, float]:
    """usagetype -> USD for every non-zero on-demand QuickSight SKU."""
    client = boto3.client("pricing", region_name=PRICING_API_REGION)
    out: dict[str, float] = {}
    paginator = client.get_paginator("get_products")
    for page in paginator.paginate(
            ServiceCode="AmazonQuickSight",
            Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]):
        for raw in page["PriceList"]:
            product = json.loads(raw)
            usagetype = product["product"]["attributes"].get("usagetype", "")
            for term in product["terms"].get("OnDemand", {}).values():
                for dim in term["priceDimensions"].values():
                    usd = dim["pricePerUnit"].get("USD")
                    if usd and float(usd) > 0:
                        out[usagetype] = float(usd)
    return out


def readme_states(amount: float) -> bool:
    """Is this dollar amount present in the README's Cost section?

    Matches "$3", "$0.38", "$250" and "$24,024" alike. Integral amounts must not
    match a longer number ($3 must not match $3,024), hence the boundary.
    """
    section = README.read_text().split("\n## Cost\n", 1)
    if len(section) != 2:
        raise SystemExit("README has no '## Cost' section")
    text = section[1].split("\n## ", 1)[0]
    if amount == int(amount):
        pattern = rf"\${int(amount):,}(?![\d,.])".replace(",", "[,]?")
    else:
        pattern = rf"\${amount:.2f}".replace(".", r"\.")
    return re.search(pattern, text) is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-east-1",
                    help="Region whose prices to check. Only us-east-1 is "
                         "supported, because the EXPECTED usage types below are "
                         "Region-prefixed (USE1-...) and that is the Region the "
                         "README documents.")
    args = ap.parse_args()

    # Refuse rather than silently verify nothing: with another Region every
    # USE1- lookup misses, so the script would report "all figures match" having
    # compared zero of them - a false pass, which is worse than no check.
    if args.region != "us-east-1":
        print(f"  Only us-east-1 is supported (asked for {args.region}).",
              file=sys.stderr)
        print("  The EXPECTED table keys are USE1-prefixed usage types, so any "
              "other\n  Region matches nothing and would pass vacuously. To "
              "check another\n  Region, add its usage types (e.g. EUW1-Reader-"
              "Enterprise-Month) first.", file=sys.stderr)
        return 2

    print(f"Checking README cost figures against the AWS Price List API "
          f"({args.region} list price)\n")
    try:
        live = live_prices(args.region)
    except (BotoCoreError, ClientError) as exc:
        print(f"  Could not reach the Price List API: {exc}", file=sys.stderr)
        print("  (needs pricing:GetProducts; the API is served from "
              "us-east-1/ap-south-1 only)", file=sys.stderr)
        return 2

    drift, unverifiable, unquoted = [], [], []
    print(f"  {'figure':40s} {'expected':>9s} {'AWS':>9s}  status")
    print(f"  {'-'*40} {'-'*9} {'-'*9}  ------")
    for usagetype, (label, expected) in sorted(EXPECTED.items(), key=lambda kv: kv[1][0]):
        actual = live.get(usagetype)
        if actual is None:
            unverifiable.append((label, expected, f"no {usagetype} SKU returned"))
            print(f"  {label:40s} {expected:9.2f} {'-':>9s}  NOT IN API")
            continue

        if abs(actual - expected) >= 0.005:
            # AWS's price moved away from what this script expects. THIS is the
            # staleness the README's own tests cannot see.
            drift.append((label, expected, actual))
            status = "DRIFTED"
        elif readme_states(expected):
            status = "ok"
        else:
            # Price is current, but the README does not quote this figure. Not a
            # defect: the Cost section deliberately prices a plain Author+Reader
            # deployment and only NAMES Pro/SPICE as things that change the bill.
            # Reporting this as drift would cry wolf on a correct README.
            unquoted.append(label)
            status = "ok (not quoted)"
        print(f"  {label:40s} {expected:9.2f} {actual:9.2f}  {status}")

    for label, expected in NOT_IN_API.items():
        unverifiable.append((label, expected, "AWS publishes no SKU for it"))
        print(f"  {label:40s} {expected:9.2f} {'-':>9s}  NOT IN API")

    print()
    if unquoted:
        print("  Current, but not quoted in the Cost section (informational):")
        for label in unquoted:
            print(f"    - {label}")
        print()
    if unverifiable:
        print("  NOT checkable from the API - verify these by hand at")
        print("  https://aws.amazon.com/quicksight/pricing/ :")
        for label, expected, why in unverifiable:
            print(f"    - {label} (README says ${expected:,.2f}): {why}")
        print()

    if drift:
        print("  PRICES DRIFTED - the README's Cost section is now WRONG:",
              file=sys.stderr)
        for label, expected, actual in drift:
            print(f"    {label}: README/script say ${expected:,.2f}, "
                  f"AWS now charges ${actual:,.2f}", file=sys.stderr)
        print("\n  Update the Cost section in README.md AND the EXPECTED table in\n"
              "  this script, then re-run tests/test_cost_docs.py so the derived\n"
              "  totals stay consistent with the new unit prices.", file=sys.stderr)
        return 1

    print("  Every API-published figure matches current AWS pricing.")
    if unverifiable:
        print("  Figures marked NOT IN API above still need a human check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
