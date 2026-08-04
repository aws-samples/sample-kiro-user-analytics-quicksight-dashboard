#!/usr/bin/env python3
"""
Report-normalizer Lambda for the Kiro User Analytics dashboard.

Reads the raw Kiro User Activity Report CSVs **by header name** and writes a
normalized, fixed-schema copy that downstream Athena views and SPICE read.

Why this exists (the bug it fixes):
  The raw report is a CSV whose *middle* columns vary between exports: Kiro
  adds a new "<model>_messages" column whenever it launches a model, and the
  trailing `New_User` column is present in some exports and absent in others.
  Athena/Hive CSV SerDes (LazySimpleSerDe / OpenCSVSerDe) map fields by ORDINAL
  position - the header line is only skipped, never used to bind columns by
  name. Once a file's column set diverges from the table's single positional
  schema, every field at or after the first varying column is read under the
  WRONG name:
    * per-model counts get attributed to a neighbouring model (so "auto"
      usage surfaces as e.g. "claude_opus_4.7"), and
    * `new_user` reads a model count (or NULL), corrupting new-vs-returning.
  Totals stay correct only because total_messages sits BEFORE the varying
  block. No positional table can parse all the header shapes, so we parse by
  HEADER here (csv.DictReader binds by name) before Athena ever sees the data.

Scaling design (streaming + incremental):
  Each raw file is parsed independently and written to its OWN output part -
  the Lambda never holds more than one file in memory, so its footprint is flat
  regardless of how many seats/days of history exist (a prior load-all design
  hit Lambda's memory ceiling at a few thousand seats). Output is partitioned
  by export_date and is INCREMENTAL: a file whose part already exists (same key
  + ETag) is skipped, so a steady-state daily run processes only the new file.
  Dedup (latest export wins per date/user/client) is NOT done here - every
  part carries src_path + export_ts + part_id + processed_ts bookkeeping
  columns and an Athena view ranks them with ROW_NUMBER(). See
  build_views.report_tables_ddl / the dedup views. This keeps the set-based
  dedup in SQL (where it scales) and the header-binding in compute (the one
  thing SQL cannot do).

What it writes (both fixed-schema FOREVER, regardless of how many models Kiro
adds):
  normalized/facts/export_date=YYYY-MM-DD/part-<hash>.csv
      one row per (activity_date, user_id, client_type) per source file, with
      the stable scalar columns + the bookkeeping columns. Header-keyed, so
      source column order never matters.
  normalized/models/export_date=YYYY-MM-DD/part-<hash>.csv
      LONG form: one row per (activity_date, user_id, client_type, model_name,
      messages) + the bookkeeping columns. model_name is a VALUE, not a column -
      so a new Kiro model adds ROWS, never a column, and the schema is stable.
  <hash> is sha256(source key | ETag), so re-running over unchanged files
  rewrites the same parts (idempotent) while changed content lands in a new
  part - see _BOOKKEEPING for how the dedup then supersedes the old one.

Each part is CSV with a header line; the Athena external tables over them use
OpenCSVSerDe with skip.header.line.count=1 and partition projection on
export_date. Positional parsing is safe DOWNSTREAM because WE author these
parts with a fixed, known column order.

Fail-closed, per FILE not per RUN: each file is normalized inside its own
try/except, so one malformed export cannot stop the others. Failures are
collected and the invocation then raises, which keeps the run visibly failed
(non-zero Lambda Errors, so an alarm can fire) while still landing every file
that parsed. That combination matters - isolating the bad file must not silently
turn a broken run into a successful one, and letting the exception escape mid-loop
used to leave every LATER file unprocessed on this and all future runs.

We NEVER delete existing output - this Lambda issues no S3 delete of any kind, by
design, because it runs against a customer's own bucket. Partially processed runs
are safe because each file's part is written atomically and the Athena dedup view
only ever surfaces the latest export per key. A superseded
part therefore lingers as unreachable storage rather than being removed;
reclaiming it (S3 lifecycle, or manual clean-up + REPROCESS_ALL) is a customer
decision, and the normalized output is fully regenerable from the raw exports.

Environment variables (set by cfn/01-data-layer.yaml):
  RAW_BUCKET     bucket holding the raw Kiro export CSVs
  RAW_PREFIX     key prefix under which the daily Kiro export CSVs live
                 (the user_report path within the logs bucket, sans bucket)
  OUT_BUCKET     bucket to write normalized output to (the Athena results
                 bucket)
  OUT_PREFIX     key prefix for normalized output (e.g. "normalized")
  REPROCESS_ALL  optional; "1"/"true" forces every file to be re-parsed even
                 if its part already exists (use after a parsing-logic change).
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.config import Config

_BOTO_CFG = Config(retries={"max_attempts": 8, "mode": "adaptive"})

# --- Source schema knowledge -------------------------------------------------
# The stable scalar columns we carry through to normalized/facts/, matched
# case-insensitively against the source header (Kiro ships mixed case, e.g.
# UserId, ProfileId). `email` appears only in post-April-2026 exports and is
# tolerated when absent.
_FACT_COLUMNS = [
    "date",
    "userid",
    "client_type",
    "chat_conversations",
    "credits_used",
    "overage_cap",
    "overage_credits_used",
    "overage_enabled",
    "profileid",
    "subscription_tier",
    "total_messages",
    "new_user",
    "email",
]

# Bookkeeping columns appended to EVERY normalized row so the Athena dedup view
# can pick the latest export per key.
#
# part_id identifies the PART this row was written into, and is what makes the
# dedup deterministic. A part is named sha256(source key | ETag), so when a
# source CSV is re-exported IN PLACE with different content it gets a NEW part
# name and the previous part is orphaned rather than overwritten - both then sit
# in the same export_date partition describing the same (date,user,client) keys.
# src_path and export_ts are IDENTICAL across those two parts (same source
# file), so ranking on them alone was a tie, with two consequences:
#   * report_facts picked a winner arbitrarily, so a query could return the
#     STALE row (non-deterministic between identical queries).
#   * report_models joined on src_path, which matches BOTH parts, so per-model
#     messages were DOUBLE-COUNTED.
# part_id breaks the tie deterministically, and because it is unique per part it
# also lets the models view join to exactly one part. We do not delete the
# superseded part - nothing in this pipeline deletes from a customer bucket -
# it simply becomes unreachable.
#
# processed_ts (UTC, YYYYMMDDHHMMSS - lexically sortable) is WHEN this part was
# written, which is the only ordering that actually means "newer content": the
# part_id hash is unordered, and export_ts comes from the source filename so it
# is equal for both parts. Ranking on processed_ts DESC therefore prefers the
# most recently normalized version of a re-exported file.
_BOOKKEEPING = ["src_path", "export_ts", "part_id", "processed_ts"]

# Fixed output headers (data columns + bookkeeping). Order is authoritative -
# the external-table DDL in build_views.py must match exactly.
_FACT_OUT_HEADER = _FACT_COLUMNS + _BOOKKEEPING
_MODEL_OUT_HEADER = [
    "date",
    "userid",
    "client_type",
    "subscription_tier",
    "model_name",
    "messages",
] + _BOOKKEEPING

# Extract an export timestamp + date from the Kiro key path. Kiro writes
# .../us-east-1/YYYY/MM/DD/HH/KIRO_..._user_report_YYYYMMDDHHMM.csv - we use the
# YYYYMMDDHHMM suffix when present (most precise), else the YYYY/MM/DD/HH path.
_TS_SUFFIX_RE = re.compile(r"_(\d{12})\.csv$", re.IGNORECASE)
_PATH_DATE_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/(\d{2})/")


def _s3():
    return boto3.client("s3", config=_BOTO_CFG)


def _norm_key(header: str) -> str:
    """Canonicalise a raw CSV header cell for matching: strip + lower-case."""
    return header.strip().lower()


def _is_model_column(header_key: str) -> bool:
    """A per-model column ends with '_messages' and is not the grand total."""
    return header_key.endswith("_messages") and header_key != "total_messages"


def _model_name(header_key: str) -> str:
    """'auto_messages' -> 'auto', 'claude_opus_4.7_messages' -> 'claude_opus_4.7'."""
    return header_key[: -len("_messages")]


def _to_int(value: str) -> int:
    """Parse a count cell to a non-negative int; blanks/garbage -> 0."""
    if value is None:
        return 0
    v = value.strip()
    if not v:
        return 0
    try:
        return max(0, int(float(v)))  # tolerate "12.0" style floats
    except (ValueError, TypeError, OverflowError):
        # OverflowError matters: float("inf") / "1e400" parse fine as floats but
        # int() of them raises, and an escaping exception here aborts the whole
        # run - not just this file - leaving every later file unprocessed on
        # every future run (the source ETag never changes, so it never
        # self-heals). Treat a non-finite count as garbage, like any other.
        return 0


def _export_ts(source_key: str) -> str:
    """A sortable export timestamp string for the source file. Prefers the
    YYYYMMDDHHMM filename suffix; falls back to the YYYY/MM/DD/HH path; else ''
    (such a file sorts oldest, so a properly-stamped re-export still wins)."""
    m = _TS_SUFFIX_RE.search(source_key)
    if m:
        return m.group(1)  # YYYYMMDDHHMM
    m = _PATH_DATE_RE.search(source_key)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}00"
    return ""


def _export_date(source_key: str) -> str:
    """The partition value YYYY-MM-DD for this file, from its export_ts."""
    ts = _export_ts(source_key)
    if len(ts) >= 8:
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
    return "unknown"


def _part_name(source_key: str, etag: str) -> str:
    """Deterministic part filename for a (source key, ETag) pair. Including the
    ETag means a re-exported file with NEW content gets a NEW part (self-
    healing), while an unchanged file maps to the same part (idempotent skip)."""
    h = hashlib.sha256(f"{source_key}|{etag}".encode("utf-8")).hexdigest()[:16]
    return f"part-{h}.csv"


def _list_csv_objects(s3, bucket: str, prefix: str) -> list[dict]:
    """All .csv objects under prefix (recursive), each as {Key, ETag}."""
    objs: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".csv"):
                objs.append({"Key": obj["Key"], "ETag": obj.get("ETag", "").strip('"')})
    return objs


def _exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def _parse_file(body: str, source_key: str, part_id: str,
                processed_ts: str) -> tuple[list, list]:
    """Parse one raw CSV (header-keyed) into (fact_rows, model_rows) for this
    file only. Column ORDER and the presence/absence of any model column or
    New_User are irrelevant - every value is looked up by header name, which is
    what makes this immune to the positional bug. No cross-file dedup here -
    that happens in the Athena views via the bookkeeping columns.

    part_id / processed_ts identify and time-stamp the part these rows land in;
    see _BOOKKEEPING for why the dedup needs both."""
    reader = csv.DictReader(io.StringIO(body))
    if reader.fieldnames is None:
        return [], []
    key_to_header = {_norm_key(h): h for h in reader.fieldnames if h is not None}
    model_keys = [k for k in key_to_header if _is_model_column(k)]

    src = source_key
    ts = _export_ts(source_key)
    book = [src, ts, part_id, processed_ts]
    fact_rows: list = []
    model_rows: list = []

    for row in reader:
        def get(canon: str) -> str:
            hdr = key_to_header.get(canon)
            return row.get(hdr, "") if hdr is not None else ""

        date_v = get("date")
        user_v = get("userid")
        client_v = get("client_type")
        tier_v = get("subscription_tier")

        fact_rows.append([get(c) for c in _FACT_COLUMNS] + book)

        for mk in model_keys:
            msgs = _to_int(row.get(key_to_header[mk], ""))
            if msgs > 0:
                model_rows.append(
                    [date_v, user_v, client_v, tier_v, _model_name(mk), msgs] + book
                )
    return fact_rows, model_rows


def _write_csv(s3, bucket: str, key: str, header: list, rows: list) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )


def handler(event, context):  # noqa: ARG001 - Lambda signature
    raw_bucket = os.environ["RAW_BUCKET"]
    raw_prefix = os.environ["RAW_PREFIX"]
    out_bucket = os.environ["OUT_BUCKET"]
    out_prefix = os.environ["OUT_PREFIX"].strip("/")
    reprocess_all = os.environ.get("REPROCESS_ALL", "").strip().lower() in ("1", "true", "yes")

    s3 = _s3()
    objects = _list_csv_objects(s3, raw_bucket, raw_prefix)

    # Stamped once per invocation: every part written by this run shares it, and
    # a later run always has a strictly greater value. That is what lets the
    # dedup views prefer the most recently normalized part when a source file
    # was re-exported in place (see _BOOKKEEPING).
    processed_ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    processed = 0
    skipped = 0
    fact_total = 0
    model_total = 0
    failures: list[dict] = []

    for obj in objects:
        src_key = obj["Key"]
        # ONE file's failure must not abort the run. Objects are iterated in S3
        # lexicographic order, so without this guard an exception on file N left
        # files N+1..end unprocessed - and because the skip sentinel is keyed on
        # (key, ETag) and the source ETag never changes, EVERY later run failed
        # at the same file too. The pipeline stopped permanently, silently: it
        # "fails closed", so the dashboard kept serving yesterday's data rather
        # than showing an error. One malformed export could freeze the dashboard
        # indefinitely with nobody told. (Reproduced with an unterminated quote
        # in a >128KB file -> _csv.Error, a non-UTF-8 byte -> UnicodeDecodeError,
        # and a non-finite count -> OverflowError.)
        #
        # Catching per file makes the module docstring's "a per-file failure
        # aborts that file" claim actually true. We still RAISE at the end so the
        # invocation is visibly failed (Lambda Errors metric, alarms, retries) -
        # isolating the bad file must not silently downgrade a broken run to a
        # successful one.
        try:
            etag = obj["ETag"]
            export_date = _export_date(src_key)
            part = _part_name(src_key, etag)
            facts_key = f"{out_prefix}/facts/export_date={export_date}/{part}"
            models_key = f"{out_prefix}/models/export_date={export_date}/{part}"

            # Incremental skip: a part keyed by (src_key, ETag) already existing
            # means this exact file content was normalized before. A re-exported
            # file with changed content has a different ETag -> new part -> it is
            # reprocessed (self-healing). The superseded part is left in place (we
            # never delete from a customer bucket); the dedup views rank on
            # processed_ts so only the newest part is reachable.
            if not reprocess_all and _exists(s3, out_bucket, facts_key):
                skipped += 1
                continue

            body = s3.get_object(Bucket=raw_bucket, Key=src_key)["Body"].read().decode(
                "utf-8-sig"  # tolerate a UTF-8 BOM on the header cell
            )
            fact_rows, model_rows = _parse_file(body, src_key, part, processed_ts)

            # Write models first then facts: the facts part is the skip sentinel
            # (checked above), so writing it LAST means a mid-file crash never
            # leaves a facts part without its matching models part.
            _write_csv(s3, out_bucket, models_key, _MODEL_OUT_HEADER, model_rows)
            _write_csv(s3, out_bucket, facts_key, _FACT_OUT_HEADER, fact_rows)

            processed += 1
            fact_total += len(fact_rows)
            model_total += len(model_rows)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see above
            # Log the key and the exception TYPE only. The message can echo file
            # content (csv errors quote the offending field), and this log group
            # is not PII-scoped.
            failures.append({"key": src_key, "error": type(exc).__name__})
            print(f"FAILED to normalize {src_key}: {type(exc).__name__}")

    result = {
        "files_seen": len(objects),
        "files_processed": processed,
        "files_skipped": skipped,
        "files_failed": len(failures),
        "fact_rows_written": fact_total,
        "model_rows_written": model_total,
    }

    if failures:
        # Every other file has been normalized and its parts are durable, so the
        # good data is already in place; this raise exists to make the failure
        # OBSERVABLE (non-zero Lambda Errors -> alarm) rather than to undo work.
        # A file listed here will be retried on the next scheduled run, and will
        # keep failing until the source file is corrected - so the alarm is the
        # signal to go and look at it.
        raise RuntimeError(
            f"Normalized {processed} file(s), skipped {skipped}, "
            f"FAILED {len(failures)}: "
            + ", ".join(f"{f['key']} ({f['error']})" for f in failures[:10])
            + (" ..." if len(failures) > 10 else "")
        )

    return result
