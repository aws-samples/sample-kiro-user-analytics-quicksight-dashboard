# Tests

Fast, offline tests for the pieces of this solution that produce **plausible but
wrong output** when they break. Every historical bug in this repo was silent — a
number that looked reasonable and wasn't — so these tests target the invariants
whose violation is invisible on the dashboard.

No AWS account, no credentials, no network, no deployment. They run in a couple
of seconds.

## Running them

```bash
python3 -m unittest discover -s tests -v      # from the repo root
scripts/run-checks.sh                          # tests + shell/CFN/Python linting
```

`unittest` ships with Python, so there is nothing to install. `run-checks.sh`
additionally uses `shellcheck` and `cfn-lint` when they are present and skips
them (with a notice) when they are not.

## What each file covers, and which real bug it would have caught

| File | Covers | Bug it guards against |
|---|---|---|
| `test_normalizer.py` | Header-keyed parsing, `_to_int`, `_export_ts`/`_export_date`, `_part_name` | Positional parsing misattributing per-model usage as Kiro's CSV header drifts; a non-finite cell aborting the whole run |
| `test_tier_labels.py` | `tier_label_expr` substitution | A `CASE` whose `ELSE` returned a *different* expression than the one tested, which made a per-user-constant column non-constant and split users into multiple rows |
| `test_sql_render.py` | Every view renders, splits into one statement, and leaves no placeholder | A `;` inside a `--` comment truncating a view; a new placeholder added to one branch only |
| `test_view_order.py` | Views are applied in dependency order by filename sort | Adding `00a_*.sql` that reads `base_user_activity` would fail on a clean deploy but succeed on a redeploy — the worst possible failure signature |
| `test_dataset_inventory.py` | `deploy.sh`'s dataset lists match the CloudFormation template | A hardcoded list drifting from the template, which left resolved PII in a SPICE dataset that the opt-out never purged |
| `test_iam_policy.py` | Bucket-name validation, additive-policy semantics | An unvalidated bucket name rendering `arn:aws:s3:::*/*` onto the shared QuickSight role; a narrowing re-apply silently not narrowing |
| `test_dashboard_definition.py` | Dataset references resolve, drill-through target, tier filter/display agreement | Filtering `subscription_tier` while grouping `user_tier`; a visual referencing a dataset that no longer exists |

## Conventions

Tests import the scripts directly from `scripts/` — there is no package to
install. Anything requiring AWS is out of scope by design: the point is that a
contributor can run these before opening a PR without an account.

## Are these tests actually load-bearing?

Verified by mutation: each of the five defects this repo has shipped was
reintroduced into a clean copy of the tree, and the suite was run.

| Reintroduced defect | Result |
|---|---|
| `CASE ... ELSE <different expression>` (the tier bug a customer reported) | 3 failures |
| `_to_int` not catching `OverflowError` (one cell freezing the pipeline) | 3 errors |
| A dataset dropped from `QS_LABEL_DATASETS` (PII left in SPICE after opt-out) | 1 failure |
| Tier filter pointed at `subscription_tier` while visuals group `user_tier` | 2 failures |
| A dimension inserted before `user_label`, retargeting the drill-through | 2 failures |

The suite returns to green when each mutation is reverted. A test that cannot
fail is not protecting anything, so this check is worth repeating if the suite is
substantially rewritten.
