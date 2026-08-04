# Changelog

All notable changes to this sample are recorded here. The version in
[`VERSION`](./VERSION) is the single source of truth; `scripts/deploy.sh` stamps
it onto every stack as the `KiroAnalyticsVersion` tag and onto each QuickSight
dashboard version description, so a deployed dashboard can always be traced back
to the code that produced it.

This project follows [Semantic Versioning](https://semver.org/) with the
practical reading below, since the deliverable is a deployment, not a library:

| Bump | Means |
|---|---|
| **MAJOR** | A re-deploy is not enough. Manual action is required (a teardown, a parameter you must now set, a resource you must delete by hand), or a number on the dashboard changes meaning. |
| **MINOR** | New sheets, visuals, datasets or opt-in features. A plain re-deploy picks it up. |
| **PATCH** | Fixes and documentation. A plain re-deploy picks it up. |

## Which version am I running?

```bash
# Deployed version (any stack carries the tag):
aws cloudformation describe-stacks --stack-name <prefix>-data \
    --query "Stacks[0].Tags[?Key=='KiroAnalyticsVersion'].Value" --output text

# Version in this checkout:
cat VERSION
```

If that returns nothing, the deployment predates versioning (anything before
1.0.0). Identify it from what you can see on the dashboard instead:

| What you observe | Your deployment predates |
|---|---|
| Five sheets, including an **Executive** sheet | 1.0.0 (#11, 2026-06-12) |
| An **AWS Glue crawler** in the stack rather than a `normalize-report` Lambda | 1.0.0 (#10, 2026-06-08) |
| Tier reads **`PRO_PLUS`** rather than **`Pro+`** | 1.0.0 (#17, 2026-07-28) |
| A single user occupying **more than one row** in All Users, or Economics tier slices summing to more than the total | 1.0.0 (#23, 2026-07-31) |
| Per-model credits roughly **double** the per-user total | 1.0.0 (#18, 2026-07-28) |
| No per-user-per-day grid on the **People** sheet | 1.0.0 (#15, 2026-07-27) |

In every case the fix is `git pull` then re-run `scripts/deploy.sh` — see
[Upgrading](./README.md#upgrading) in the README, and note the SPICE-refresh step
there, because a view change is **not** visible until the data is re-ingested.

## [1.0.0] - 2026-08-04

First versioned release. Functionally this is the state reached by PR #29; the
version number is new, not the code. Everything below happened before this
release and is recorded so an existing deployment can be placed in history.

### Added

- Offline test suite (83 tests) and GitHub Actions CI covering the bug classes
  that fail *silently* on the dashboard: dataset-inventory drift, tier-label
  handling, per-user-constant columns, view creation order, IAM policy shape and
  dashboard-definition structure. Runs with no AWS account, no credentials and no
  network, so it works on fork PRs. `scripts/run-checks.sh` runs the same checks
  locally. (#28, #29)
- CloudWatch alarms for the report normalizer, notifying an optional
  `ALARM_EMAIL`: one for invocation errors, one for the function not running at
  all. The second matters because the pipeline fails closed — without it a broken
  normalizer is indistinguishable from a quiet week. (#25)
- `LOG_RETENTION_DAYS` (default 30) on the normalizer's log group, which Lambda
  auto-creates with *no* expiry. (#25)
- Per-user-per-day usage grid on the People sheet, as a pivot table — users as
  rows, dates as columns — carrying the Identity Center username as a join key
  so an export can be matched against Kiro's own subscription export. (#15, #16,
  #21)
- Optional IAM Identity Center user mapping, resolving the report's opaque
  `user_id` GUIDs to names and emails, with a documented opt-out path that purges
  resolved names from SPICE. (#12, #13, #14)
- Support for a Kiro logs bucket in a different Region from the dashboard,
  verified end to end including the SSE-KMS case. (#19)

### Fixed

- **Tier labels were inconsistent** — the same tier rendered as both `PRO_PLUS`
  and `Pro+` depending on the visual. All tier rendering now goes through one
  shared expression. (#17)
- **A user could occupy several rows instead of one.** `user_label` and
  `user_tier` were computed per row rather than per user, so anyone who changed
  tier mid-window — or whose email arrived partway through the window — was split
  across rows, and each row carried only part of their usage. On Economics the
  tier slices could therefore sum to more than the chart's own total. (#23)
- **Per-model credits were double-counted**, because the model join keyed on
  source path rather than part id. (#18)
- **One malformed export could freeze the whole pipeline.** The normalizer now
  isolates failures per file, keeps going, and still exits non-zero so the run
  stays visibly failed rather than silently partial. (#24)
- **Truncated CSV rows produced a silent under-count**: a short row yielded the
  literal string `"None"`, which Athena cast to 0. Found by the test suite. (#28)
- **Turning identity mapping off left real names in SPICE** while reporting
  success, because two hardcoded dataset lists in `deploy.sh` had drifted from
  the CloudFormation template. Both are now checked against the template by a
  test. (#22)
- **Deploying a second stack silently revoked the first stack's QuickSight S3
  access**, because all deployments share one QuickSight service role and the
  inline policy name was fixed. The policy is now namespaced per
  `STACK_PREFIX`. (#20)
- Week-over-week movers compared a 7-day window against an overlapping one, so
  one day fell in neither. The windows are now equal, adjacent and
  non-overlapping. (#22)
- Rows with an unparseable date silently became "today" via a lenient cast; they
  are now excluded. (#22)

### Changed

- Four sheets — Activity & Trends, Economics, People, User detail — replacing the
  earlier five. The Executive sheet duplicated numbers shown elsewhere. (#11)
- The AWS Glue crawler was replaced by a header-keyed normalizer Lambda, so a
  column reordering in the Kiro export no longer silently shifts every value into
  the wrong field. (#10)
- Athena query results now expire after 7 days rather than 30, and noncurrent
  versions after 1 day. Query results contain resolved names and emails when
  identity mapping is on, so the retention window is the exposure window. (#26)
- One hue, one meaning across tier, client and model palettes, so a given tier is
  the same colour on every sheet. (#8, #9)

### Removed

- 13 unused visual builders and 2 unused datasets, with their Athena views and
  refresh schedules. Verified by byte-comparing the generated dashboard
  definition before and after. (#27)

[1.0.0]: https://github.com/aws-samples/sample-kiro-user-analytics-quicksight-dashboard/releases/tag/v1.0.0
