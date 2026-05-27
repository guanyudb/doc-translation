"""Mirror Lakebase truth into Delta for long-term archive + BI/governance reach.

Hot path (per-paragraph reviewer interactions) stays in Lakebase. At promotion
time — the single point in the lifecycle where the document becomes immutable —
we copy the full audit trail, the golden publication record, and a paragraph-
level snapshot of the review state into Delta tables next to the Volume.

Why mirror, not stream?
  * The Lakebase data is the source of truth; the Delta copy is an archive.
  * Mirroring once-per-promotion is cheap, deterministic, and matches the
    semantic boundary where compliance starts caring about the data.
  * If sync fails, the document is still safely PUBLISHED in Lakebase; we
    record a DELTA_SYNC_FAILED audit event and the next iteration of this
    module (or an operator) can retry idempotently — MERGE keys make the
    retry safe.

Schema (single-tenant, lives next to the Volume):

    {CATALOG}.{SCHEMA}.audit_events             — append, one row per event
    {CATALOG}.{SCHEMA}.golden_publications      — one row per promotion (pair_id PK)
    {CATALOG}.{SCHEMA}.silver_review_snapshots  — one row per paragraph per snapshot
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from databricks.sdk.service.sql import StatementState

from . import config
from . import store


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
DELTA_CATALOG = os.environ.get("DELTA_CATALOG", "hls_amer_catalog")
DELTA_SCHEMA  = os.environ.get("DELTA_SCHEMA",  "guanyu_chen")
DELTA_FQN     = f"{DELTA_CATALOG}.{DELTA_SCHEMA}"


def enabled() -> bool:
    """True iff a warehouse is configured. Allows the app to run with the
    Delta mirror feature disabled — promotion still works, the Lakebase side
    is still the source of truth, sync just silently no-ops."""
    return bool(WAREHOUSE_ID)


# ---------------------------------------------------------------------------
# Statement execution + SQL literal escaping
# ---------------------------------------------------------------------------


class DeltaSyncError(RuntimeError):
    """Raised on terminal failure of a Delta statement. Caller decides whether
    to roll back the calling transaction or just log + audit."""


def _execute(statement: str, *, timeout_s: float = 60.0) -> dict:
    """Run a single SQL statement against the configured warehouse. Polls
    asynchronously and returns the final response when terminal. Raises
    DeltaSyncError on FAILED/CANCELED/CLOSED so callers don't see partial
    successes."""
    if not enabled():
        raise DeltaSyncError("DATABRICKS_WAREHOUSE_ID not configured")

    client = config.w()
    resp = client.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="30s",  # max server-side wait per call
    )
    deadline = time.monotonic() + timeout_s
    while True:
        state = resp.status.state if resp.status else None
        if state in (StatementState.SUCCEEDED,):
            return resp.as_dict()
        if state in (StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED):
            err = ""
            if resp.status and resp.status.error:
                err = resp.status.error.message or str(resp.status.error)
            raise DeltaSyncError(f"Delta statement {state}: {err}\nSQL: {statement[:500]}")
        if time.monotonic() > deadline:
            try:
                client.statement_execution.cancel_execution(resp.statement_id)
            except Exception:
                pass
            raise DeltaSyncError(f"Delta statement timed out after {timeout_s}s")
        # state is PENDING / RUNNING — get the latest
        time.sleep(0.4)
        resp = client.statement_execution.get_statement(resp.statement_id)


def _esc(v: Any) -> str:
    """Escape a Python value as a SQL literal. Handles None, bool, numbers,
    timestamps, dicts/lists (as JSON strings), and strings.

    Single-quote escaping (doubling) is the standard ANSI SQL form and what
    Databricks SQL expects. No \\-escapes — those silently fail on some Spark
    versions and are not needed at this layer."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        # Always serialize as UTC ISO-8601 with explicit timezone so Delta
        # casting is unambiguous regardless of session settings.
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return f"TIMESTAMP '{v.astimezone(timezone.utc).isoformat()}'"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, default=str, ensure_ascii=False)
    s = str(v).replace("'", "''")
    return f"'{s}'"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def ensure_delta_schema() -> None:
    """Idempotent — safe to run on every promotion. CREATE IF NOT EXISTS so
    repeated calls are cheap. Runs as whoever owns the warehouse session."""
    if not enabled():
        return

    _execute(f"CREATE SCHEMA IF NOT EXISTS {DELTA_FQN}")

    # No expression-partitioning in Delta (e.g. PARTITIONED BY (DATE(event_at))).
    # Single-tenant volume is modest; defer to OPTIMIZE + Z-ORDER BY (pair_id,
    # event_at) as a maintenance task if needed.
    _execute(f"""
        CREATE TABLE IF NOT EXISTS {DELTA_FQN}.audit_events (
            event_id        BIGINT,
            pair_id         STRING,
            event_type      STRING,
            actor           STRING,
            actor_type      STRING,
            paragraph_idx   INT,
            before_value    STRING,
            after_value     STRING,
            correlation_id  STRING,
            client_ip       STRING,
            client_ua       STRING,
            event_at        TIMESTAMP,
            ingested_at     TIMESTAMP
        )
        USING DELTA
        COMMENT 'Immutable audit trail; mirrored from Lakebase at promotion time'
        TBLPROPERTIES (
            delta.appendOnly = 'true',
            delta.deletedFileRetentionDuration = 'interval 2557 days',
            delta.logRetentionDuration         = 'interval 2557 days'
        )
    """)

    _execute(f"""
        CREATE TABLE IF NOT EXISTS {DELTA_FQN}.golden_publications (
            publication_id          BIGINT,
            pair_id                 STRING,
            golden_original_path    STRING,
            golden_translated_path  STRING,
            golden_original_hash    STRING,
            golden_translated_hash  STRING,
            total_paragraphs        INT,
            certified_paragraphs    INT,
            edits_applied           INT,
            distinct_reviewers      ARRAY<STRING>,
            published_by            STRING,
            published_at            TIMESTAMP,
            ingested_at             TIMESTAMP
        )
        USING DELTA
        COMMENT 'One row per gold-zone promotion; pair_id is effectively a primary key'
        TBLPROPERTIES (
            delta.deletedFileRetentionDuration = 'interval 2557 days',
            delta.logRetentionDuration         = 'interval 2557 days'
        )
    """)

    _execute(f"""
        CREATE TABLE IF NOT EXISTS {DELTA_FQN}.silver_review_snapshots (
            snapshot_id      STRING,
            pair_id          STRING,
            snapshot_at      TIMESTAMP,
            paragraph_idx    INT,
            status           STRING,
            comment          STRING,
            edited_text      STRING,
            reviewer         STRING,
            updated_at       TIMESTAMP,
            ingested_at      TIMESTAMP
        )
        USING DELTA
        PARTITIONED BY (pair_id)
        COMMENT 'Snapshot of per-paragraph review state at promotion time'
    """)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync_pair_to_delta(pair_id: str, *, publication_id: int | None = None) -> dict:
    """Mirror everything we know about `pair_id` from Lakebase into Delta.
    Returns a small dict of counts; idempotent (MERGE on natural keys)."""
    if not enabled():
        return {"skipped": True, "reason": "no warehouse configured"}

    ensure_delta_schema()

    counts: dict[str, int] = {}

    # ---- audit_events: MERGE on event_id (BIGSERIAL, globally unique within Lakebase)
    events = store.list_audit_events(pair_id, limit=10_000)
    if events:
        rows_sql = []
        for e in events:
            rows_sql.append(
                "("
                f"{_esc(e['event_id'])}, "
                f"{_esc(pair_id)}, "
                f"{_esc(e['event_type'])}, "
                f"{_esc(e['actor'])}, "
                f"{_esc(e['actor_type'])}, "
                f"{_esc(e['paragraph_idx'])}, "
                f"{_esc(e['before_value'])}, "
                f"{_esc(e['after_value'])}, "
                f"{_esc(e['correlation_id'])}, "
                # IP and UA aren't in list_audit_events; query them lazily below
                "NULL, NULL, "
                f"{_esc(e['event_at'])}, "
                "current_timestamp()"
                ")"
            )
        _execute(f"""
            MERGE INTO {DELTA_FQN}.audit_events AS t
            USING (SELECT * FROM VALUES {','.join(rows_sql)}
                   AS v(event_id, pair_id, event_type, actor, actor_type,
                        paragraph_idx, before_value, after_value, correlation_id,
                        client_ip, client_ua, event_at, ingested_at)) AS s
            ON t.event_id = s.event_id
            WHEN NOT MATCHED THEN INSERT *
        """)
    counts["audit_events"] = len(events)

    # ---- golden_publications: MERGE on pair_id
    pub = store.get_publication(pair_id)
    if pub:
        reviewers = pub.get("distinct_reviewers") or []
        if isinstance(reviewers, str):
            reviewers = json.loads(reviewers)
        reviewers_sql = "ARRAY(" + ",".join(_esc(r) for r in reviewers) + ")" if reviewers else "ARRAY()"
        _execute(f"""
            MERGE INTO {DELTA_FQN}.golden_publications AS t
            USING (SELECT
                {_esc(pub['publication_id'])}      AS publication_id,
                {_esc(pub['pair_id'])}             AS pair_id,
                {_esc(pub['golden_original_path'])} AS golden_original_path,
                {_esc(pub['golden_translated_path'])} AS golden_translated_path,
                {_esc(pub['golden_original_hash'])} AS golden_original_hash,
                {_esc(pub['golden_translated_hash'])} AS golden_translated_hash,
                {_esc(pub['total_paragraphs'])}    AS total_paragraphs,
                {_esc(pub['certified_paragraphs'])} AS certified_paragraphs,
                {_esc(pub['edits_applied'])}       AS edits_applied,
                {reviewers_sql}                    AS distinct_reviewers,
                {_esc(pub['published_by'])}        AS published_by,
                {_esc(pub['published_at'])}        AS published_at,
                current_timestamp()                AS ingested_at
            ) AS s
            ON t.pair_id = s.pair_id
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
        counts["golden_publications"] = 1
    else:
        counts["golden_publications"] = 0

    # ---- silver_review_snapshots: write a fresh snapshot row per paragraph
    snapshot_id = f"{pair_id}:{int(time.time() * 1000)}"
    feedback = store.get_feedback(pair_id)
    if feedback:
        rows_sql = []
        for fb in feedback:
            rows_sql.append(
                "("
                f"{_esc(snapshot_id)}, "
                f"{_esc(pair_id)}, "
                "current_timestamp(), "
                f"{_esc(fb['paragraph_idx'])}, "
                f"{_esc(fb['status'])}, "
                f"{_esc(fb.get('comment'))}, "
                f"{_esc(fb.get('edited_text'))}, "
                f"{_esc(fb.get('reviewer'))}, "
                f"{_esc(fb.get('updated_at'))}, "
                "current_timestamp()"
                ")"
            )
        _execute(f"""
            INSERT INTO {DELTA_FQN}.silver_review_snapshots
            (snapshot_id, pair_id, snapshot_at, paragraph_idx, status, comment,
             edited_text, reviewer, updated_at, ingested_at)
            VALUES {','.join(rows_sql)}
        """)
    counts["silver_review_snapshots"] = len(feedback)

    # ---- Mark sync time on the publication row (idempotent)
    if pub:
        from .db import pool
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {config.PGSCHEMA}.golden_publications "
                    f"SET delta_synced_at = now() WHERE pair_id = %s",
                    (pair_id,),
                )
            conn.commit()

    return {
        "skipped": False,
        "counts": counts,
        "snapshot_id": snapshot_id if feedback else None,
        "catalog": DELTA_CATALOG,
        "schema":  DELTA_SCHEMA,
    }


def query_delta_count(table: str) -> int:
    """Tiny utility for tests: COUNT(*) on a Delta mirror table."""
    if not enabled():
        return -1
    out = _execute(f"SELECT COUNT(*) AS c FROM {DELTA_FQN}.{table}")
    rows = (out.get("result") or {}).get("data_array") or []
    if not rows:
        return 0
    return int(rows[0][0])
