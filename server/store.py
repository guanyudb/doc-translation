"""Lakebase Postgres CRUD for the doc-translation review app.

Compliance posture:
  * `audit_events` is the single source of truth for who-did-what-when.
    Every state-changing function in this module appends a row to it inside
    the same transaction as the change. The SP has INSERT but not UPDATE/DELETE.
  * `review_pairs.lifecycle_state` is the document's current state. Writes are
    rejected once the document is PUBLISHED — that's the compliance lock.
  * `golden_publications` is the immutable record of certification + promotion.
"""
from __future__ import annotations
import json
from . import config
from .db import pool


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PairLockedError(RuntimeError):
    """Raised when a write is attempted against a document that has been
    promoted to the golden zone. The reviewer sees this in the UI as a banner."""
    def __init__(self, pair_id: str, state: str):
        self.pair_id = pair_id
        self.state = state
        super().__init__(
            f"{pair_id} is in state {state}; writes are not permitted. "
            f"To re-open for review, an administrator must explicitly transition it back."
        )


# ---------------------------------------------------------------------------
# Schema migration — idempotent, run by table owner (not the SP)
# ---------------------------------------------------------------------------


def ensure_schema() -> None:
    """Create or evolve the doc_translation schema. Safe to run repeatedly.
    Must be run by the user that owns the existing tables (the SP cannot ALTER)."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # ---- review_feedback edited_text (legacy) ----
            cur.execute(f"ALTER TABLE {s}.review_feedback ADD COLUMN IF NOT EXISTS edited_text TEXT")
            # `last_published_text` is the snapshot of edited_text the last
            # time a reviewed DOCX was published. Used to distinguish edits
            # that have been baked into a published file (no longer pending)
            # from edits the reviewer has made since the last publish.
            cur.execute(f"ALTER TABLE {s}.review_feedback ADD COLUMN IF NOT EXISTS last_published_text TEXT")

            # ---- review_pairs lifecycle + content addressing ----
            cur.execute(f"""
                ALTER TABLE {s}.review_pairs
                ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'UNDER_REVIEW'
            """)
            cur.execute(f"""
                ALTER TABLE {s}.review_pairs
                ADD COLUMN IF NOT EXISTS original_hash TEXT
            """)
            cur.execute(f"""
                ALTER TABLE {s}.review_pairs
                ADD COLUMN IF NOT EXISTS translated_hash TEXT
            """)
            cur.execute(f"""
                ALTER TABLE {s}.review_pairs
                ADD COLUMN IF NOT EXISTS source_lang TEXT
            """)
            cur.execute(f"""
                ALTER TABLE {s}.review_pairs
                ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ
            """)
            cur.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'review_pairs_lifecycle_chk'
                    ) THEN
                        ALTER TABLE {s}.review_pairs
                        ADD CONSTRAINT review_pairs_lifecycle_chk
                        CHECK (lifecycle_state IN
                            ('UNDER_REVIEW','PROMOTING','PUBLISHED','ARCHIVED'));
                    END IF;
                END $$;
            """)

            # ---- review_edit_history (legacy) ----
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.review_edit_history (
                    edit_id        BIGSERIAL PRIMARY KEY,
                    pair_id        TEXT NOT NULL REFERENCES {s}.review_pairs(pair_id) ON DELETE CASCADE,
                    paragraph_idx  INT  NOT NULL,
                    previous_text  TEXT,
                    new_text       TEXT,
                    reviewer       TEXT,
                    edited_at      TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS review_edit_history_pair_idx
                ON {s}.review_edit_history (pair_id, paragraph_idx, edited_at DESC)
            """)

            # ---- review_publish_log (legacy: reviewed DOCX writebacks) ----
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.review_publish_log (
                    publish_id      BIGSERIAL PRIMARY KEY,
                    pair_id         TEXT NOT NULL REFERENCES {s}.review_pairs(pair_id) ON DELETE CASCADE,
                    output_path     TEXT NOT NULL,
                    edits_applied   INT  NOT NULL DEFAULT 0,
                    published_by    TEXT,
                    published_at    TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS review_publish_log_pair_idx
                ON {s}.review_publish_log (pair_id, published_at DESC)
            """)

            # ---- audit_events: append-only, the ground truth for traceability ----
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.audit_events (
                    event_id        BIGSERIAL PRIMARY KEY,
                    pair_id         TEXT,
                    event_type      TEXT NOT NULL,
                    actor           TEXT NOT NULL,
                    actor_type      TEXT NOT NULL DEFAULT 'human',
                    paragraph_idx   INT,
                    before_value    JSONB,
                    after_value     JSONB,
                    correlation_id  TEXT,
                    client_ip       TEXT,
                    client_ua       TEXT,
                    event_at        TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS audit_events_pair_idx
                ON {s}.audit_events (pair_id, event_at DESC)
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS audit_events_type_idx
                ON {s}.audit_events (event_type, event_at DESC)
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS audit_events_actor_idx
                ON {s}.audit_events (actor, event_at DESC)
            """)

            # ---- golden_publications: one row per promotion (immutable) ----
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.golden_publications (
                    publication_id          BIGSERIAL PRIMARY KEY,
                    pair_id                 TEXT NOT NULL UNIQUE REFERENCES {s}.review_pairs(pair_id),
                    golden_original_path    TEXT NOT NULL,
                    golden_translated_path  TEXT NOT NULL,
                    golden_original_hash    TEXT NOT NULL,
                    golden_translated_hash  TEXT NOT NULL,
                    total_paragraphs        INT  NOT NULL,
                    certified_paragraphs    INT  NOT NULL,
                    edits_applied           INT  NOT NULL DEFAULT 0,
                    distinct_reviewers      JSONB NOT NULL DEFAULT '[]'::jsonb,
                    published_by            TEXT NOT NULL,
                    published_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
                    delta_synced_at         TIMESTAMPTZ
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS golden_publications_published_idx
                ON {s}.golden_publications (published_at DESC)
            """)

            # ---- paragraph_confidence: per-paragraph heuristic quality score,
            #      computed at first render and surfaced as a badge + filter.
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.paragraph_confidence (
                    pair_id          TEXT NOT NULL REFERENCES {s}.review_pairs(pair_id) ON DELETE CASCADE,
                    paragraph_idx    INT  NOT NULL,
                    length_ratio     REAL,
                    untranslated_pct REAL,
                    repeated_ngrams  INT,
                    confidence       REAL NOT NULL,
                    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    source_lang      TEXT,
                    target_lang      TEXT,
                    PRIMARY KEY (pair_id, paragraph_idx)
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS paragraph_confidence_score_idx
                ON {s}.paragraph_confidence (pair_id, confidence)
            """)

            # ---- translation_glossary: terminology entries used to steer future
            #      translations (Phase 1b/1c). Two kinds, distinguished by `source`:
            #        * 'tenant'          — mined from review_edit_history. model_phrase
            #                              and correction are both TARGET-language text
            #                              (what the model wrote → what reviewers wrote).
            #        * 'seed'/'customer' — bilingual pairs. model_phrase is SOURCE-language
            #                              text, correction is the required target term.
            #                              Loaded from the optional shipped seed file or
            #                              a customer's own CSV import.
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.translation_glossary (
                    entry_id            BIGSERIAL PRIMARY KEY,
                    source_lang         TEXT,
                    target_lang         TEXT,
                    model_phrase        TEXT NOT NULL,
                    correction          TEXT NOT NULL,
                    occurrences         INT  NOT NULL DEFAULT 1,
                    distinct_reviewers  INT  NOT NULL DEFAULT 1,
                    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    approved            BOOLEAN NOT NULL DEFAULT TRUE,
                    source              TEXT NOT NULL DEFAULT 'tenant',
                    UNIQUE (source_lang, target_lang, model_phrase, correction)
                )
            """)
            # Migration for deployments created before the `source` column existed.
            cur.execute(f"""
                ALTER TABLE {s}.translation_glossary
                ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'tenant'
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS translation_glossary_lookup_idx
                ON {s}.translation_glossary (source_lang, target_lang, approved, occurrences DESC)
            """)
        conn.commit()


# ---------------------------------------------------------------------------
# Audit + lock helpers
# ---------------------------------------------------------------------------


# Event type vocabulary — kept open (no DB enum) so new types can be added
# without a migration, but enforced as Python constants so typos surface fast.
class EventType:
    OPENED                       = "OPENED"
    PARAGRAPH_STATUS_CHANGED     = "PARAGRAPH_STATUS_CHANGED"
    PARAGRAPH_COMMENT_SET        = "PARAGRAPH_COMMENT_SET"
    PARAGRAPH_EDITED             = "PARAGRAPH_EDITED"
    PARAGRAPH_REVERTED           = "PARAGRAPH_REVERTED"
    BULK_STATUS_CHANGED          = "BULK_STATUS_CHANGED"
    REVIEWED_DOCX_PUBLISHED      = "REVIEWED_DOCX_PUBLISHED"
    GOLD_PROMOTION_STARTED       = "GOLD_PROMOTION_STARTED"
    GOLD_PROMOTED                = "GOLD_PROMOTED"
    GOLD_PROMOTION_FAILED        = "GOLD_PROMOTION_FAILED"
    INVALID_WRITE_BLOCKED        = "INVALID_WRITE_BLOCKED"
    LIFECYCLE_TRANSITIONED       = "LIFECYCLE_TRANSITIONED"


def _emit_audit(cur, *, pair_id: str | None, event_type: str, actor: str,
                paragraph_idx: int | None = None,
                before: dict | None = None, after: dict | None = None,
                correlation_id: str | None = None,
                client_ip: str | None = None, client_ua: str | None = None,
                actor_type: str = "human") -> None:
    """Append a row to audit_events using the caller's cursor — keeps the
    audit row inside the same transaction as the change it records."""
    cur.execute(f"""
        INSERT INTO {config.PGSCHEMA}.audit_events
            (pair_id, event_type, actor, actor_type, paragraph_idx,
             before_value, after_value, correlation_id, client_ip, client_ua)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
    """, (
        pair_id, event_type, actor, actor_type, paragraph_idx,
        json.dumps(before) if before is not None else None,
        json.dumps(after)  if after  is not None else None,
        correlation_id, client_ip, client_ua,
    ))


def _assert_unlocked(cur, pair_id: str, *, actor: str, attempted_event: str) -> None:
    """Block writes against PUBLISHED documents. The blocked attempt is itself
    audited so a tamper attempt leaves a trail."""
    cur.execute(
        f"SELECT lifecycle_state FROM {config.PGSCHEMA}.review_pairs WHERE pair_id = %s",
        (pair_id,),
    )
    row = cur.fetchone()
    state = row[0] if row else None
    if state in ("PUBLISHED", "ARCHIVED"):
        _emit_audit(cur, pair_id=pair_id, event_type=EventType.INVALID_WRITE_BLOCKED,
                    actor=actor, after={"blocked_event": attempted_event, "state": state})
        raise PairLockedError(pair_id, state)


# ---------------------------------------------------------------------------
# review_pairs
# ---------------------------------------------------------------------------


def upsert_pair(p: dict) -> None:
    """Idempotent upsert of a pair (called when a reviewer first picks it).
    `lifecycle_state` is intentionally NOT updated here — it stays at whatever
    the row already has (default UNDER_REVIEW for new rows)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.review_pairs
                    (pair_id, original_path, translated_path, target_lang,
                     source_lang, total_paragraphs,
                     original_hash, translated_hash)
                VALUES (%(pair_id)s, %(original_path)s, %(translated_path)s,
                        %(target_lang)s, %(source_lang)s, %(total_paragraphs)s,
                        %(original_hash)s, %(translated_hash)s)
                ON CONFLICT (pair_id) DO UPDATE SET
                    original_path    = EXCLUDED.original_path,
                    translated_path  = EXCLUDED.translated_path,
                    target_lang      = EXCLUDED.target_lang,
                    source_lang      = COALESCE(EXCLUDED.source_lang,
                                                {config.PGSCHEMA}.review_pairs.source_lang),
                    total_paragraphs = COALESCE(EXCLUDED.total_paragraphs,
                                                {config.PGSCHEMA}.review_pairs.total_paragraphs),
                    original_hash    = COALESCE(EXCLUDED.original_hash,
                                                {config.PGSCHEMA}.review_pairs.original_hash),
                    translated_hash  = COALESCE(EXCLUDED.translated_hash,
                                                {config.PGSCHEMA}.review_pairs.translated_hash)
            """, {
                "pair_id":          p["pair_id"],
                "original_path":    p.get("original_path"),
                "translated_path":  p.get("translated_path"),
                "target_lang":      p.get("target_lang"),
                "source_lang":      p.get("source_lang"),
                "total_paragraphs": p.get("total_paragraphs"),
                "original_hash":    p.get("original_hash"),
                "translated_hash":  p.get("translated_hash"),
            })
        conn.commit()


def record_open(pair_id: str, actor: str,
                client_ip: str | None = None, client_ua: str | None = None) -> None:
    """Audit-only: a reviewer opened this document. Idempotent per session
    by dedup at the call site (app.py only calls once per session)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.OPENED,
                        actor=actor, client_ip=client_ip, client_ua=client_ua)
        conn.commit()


def list_pairs_with_progress() -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    p.pair_id, p.original_path, p.translated_path,
                    p.target_lang, p.source_lang, p.total_paragraphs,
                    p.created_at, p.finalized_at,
                    p.lifecycle_state, p.locked_at,
                    COALESCE(SUM(CASE WHEN f.status='certified' THEN 1 ELSE 0 END), 0) AS certified,
                    COALESCE(SUM(CASE WHEN f.status='flagged'   THEN 1 ELSE 0 END), 0) AS flagged,
                    COALESCE(SUM(CASE WHEN f.comment IS NOT NULL AND f.comment <> '' THEN 1 ELSE 0 END), 0) AS commented,
                    COALESCE(SUM(CASE WHEN f.edited_text IS NOT NULL THEN 1 ELSE 0 END), 0) AS edited
                FROM {config.PGSCHEMA}.review_pairs p
                LEFT JOIN {config.PGSCHEMA}.review_feedback f ON f.pair_id = p.pair_id
                GROUP BY p.pair_id
                ORDER BY p.created_at DESC
            """)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_pair(pair_id: str) -> dict | None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT pair_id, original_path, translated_path,
                       target_lang, source_lang, total_paragraphs,
                       created_at, finalized_at, lifecycle_state, locked_at,
                       original_hash, translated_hash
                FROM {config.PGSCHEMA}.review_pairs
                WHERE pair_id = %s
            """, (pair_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))


def is_locked(pair_id: str) -> bool:
    """Cheap check used by the app to decide whether to disable write UI."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT lifecycle_state FROM {config.PGSCHEMA}.review_pairs WHERE pair_id = %s",
                (pair_id,),
            )
            row = cur.fetchone()
            return bool(row and row[0] in ("PUBLISHED", "ARCHIVED"))


# ---------------------------------------------------------------------------
# review_feedback
# ---------------------------------------------------------------------------


def get_feedback(pair_id: str) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT paragraph_idx, status, comment, reviewer, updated_at, edited_text
                FROM {config.PGSCHEMA}.review_feedback
                WHERE pair_id = %s
                ORDER BY paragraph_idx
            """, (pair_id,))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def upsert_feedback(pair_id: str, paragraph_idx: int, status: str | None,
                    comment: str | None, reviewer: str) -> dict:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _assert_unlocked(cur, pair_id, actor=reviewer,
                             attempted_event=EventType.PARAGRAPH_STATUS_CHANGED)

            cur.execute(f"""
                SELECT status, comment FROM {config.PGSCHEMA}.review_feedback
                WHERE pair_id = %s AND paragraph_idx = %s
            """, (pair_id, paragraph_idx))
            prev = cur.fetchone()
            prev_status, prev_comment = (prev[0], prev[1]) if prev else (None, None)

            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.review_feedback
                    (pair_id, paragraph_idx, status, comment, reviewer, updated_at)
                VALUES (%s, %s, COALESCE(%s, 'pending'), %s, %s, now())
                ON CONFLICT (pair_id, paragraph_idx) DO UPDATE SET
                    status     = COALESCE(EXCLUDED.status, {config.PGSCHEMA}.review_feedback.status),
                    comment    = EXCLUDED.comment,
                    reviewer   = EXCLUDED.reviewer,
                    updated_at = now()
                RETURNING paragraph_idx, status, comment, reviewer, updated_at
            """, (pair_id, paragraph_idx, status, comment, reviewer))
            cols = [d.name for d in cur.description]
            row = cur.fetchone()
            new_status, new_comment = row[1], row[2]

            if new_status != prev_status:
                _emit_audit(cur, pair_id=pair_id,
                            event_type=EventType.PARAGRAPH_STATUS_CHANGED,
                            actor=reviewer, paragraph_idx=paragraph_idx,
                            before={"status": prev_status},
                            after={"status": new_status})
            if (new_comment or "") != (prev_comment or ""):
                _emit_audit(cur, pair_id=pair_id,
                            event_type=EventType.PARAGRAPH_COMMENT_SET,
                            actor=reviewer, paragraph_idx=paragraph_idx,
                            before={"comment": prev_comment},
                            after={"comment": new_comment})
        conn.commit()
    return dict(zip(cols, row))


def bulk_upsert_feedback(
    pair_id: str,
    paragraph_idxs: list[int],
    status: str,
    reviewer: str,
    *,
    skip_commented: bool = False,
) -> int:
    if not paragraph_idxs:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _assert_unlocked(cur, pair_id, actor=reviewer,
                             attempted_event=EventType.BULK_STATUS_CHANGED)

            values_sql = ",".join(f"(%s,%s,%s,%s,%s,now())" for _ in paragraph_idxs)
            params: list = []
            for idx in paragraph_idxs:
                params.extend([pair_id, idx, status, None, reviewer])
            on_conflict_extra = (
                "AND ({s}.review_feedback.comment IS NULL OR {s}.review_feedback.comment = '')".format(s=config.PGSCHEMA)
                if skip_commented else ""
            )
            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.review_feedback
                    (pair_id, paragraph_idx, status, comment, reviewer, updated_at)
                VALUES {values_sql}
                ON CONFLICT (pair_id, paragraph_idx) DO UPDATE SET
                    status     = EXCLUDED.status,
                    reviewer   = EXCLUDED.reviewer,
                    updated_at = now()
                WHERE TRUE {on_conflict_extra}
            """, params)
            count = cur.rowcount

            _emit_audit(cur, pair_id=pair_id,
                        event_type=EventType.BULK_STATUS_CHANGED,
                        actor=reviewer,
                        after={"status": status,
                               "paragraph_idxs": paragraph_idxs,
                               "skip_commented": skip_commented,
                               "rows_affected": count})
        conn.commit()
    return count


def progress_for(pair_id: str) -> dict:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    p.total_paragraphs AS total,
                    p.lifecycle_state,
                    COALESCE(SUM(CASE WHEN f.status='certified' THEN 1 ELSE 0 END), 0) AS certified,
                    COALESCE(SUM(CASE WHEN f.status='flagged'   THEN 1 ELSE 0 END), 0) AS flagged,
                    COALESCE(SUM(CASE WHEN f.comment IS NOT NULL AND f.comment <> '' THEN 1 ELSE 0 END), 0) AS commented,
                    COALESCE(SUM(CASE WHEN f.edited_text IS NOT NULL THEN 1 ELSE 0 END), 0) AS edited
                FROM {config.PGSCHEMA}.review_pairs p
                LEFT JOIN {config.PGSCHEMA}.review_feedback f ON f.pair_id = p.pair_id
                WHERE p.pair_id = %s
                GROUP BY p.pair_id
            """, (pair_id,))
            row = cur.fetchone()
            if not row:
                return {"total": 0, "lifecycle_state": "UNDER_REVIEW",
                        "certified": 0, "flagged": 0, "commented": 0, "edited": 0}
            return {"total": row[0] or 0, "lifecycle_state": row[1] or "UNDER_REVIEW",
                    "certified": row[2], "flagged": row[3],
                    "commented": row[4], "edited": row[5]}


# ---------------------------------------------------------------------------
# Edits (live overlay) + edit history
# ---------------------------------------------------------------------------


def get_edits(pair_id: str) -> dict[int, str]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT paragraph_idx, edited_text
                FROM {config.PGSCHEMA}.review_feedback
                WHERE pair_id = %s AND edited_text IS NOT NULL
            """, (pair_id,))
            return {r[0]: r[1] for r in cur.fetchall()}


def upsert_edit(pair_id: str, paragraph_idx: int, new_text: str | None,
                reviewer: str) -> dict:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _assert_unlocked(cur, pair_id, actor=reviewer,
                             attempted_event=(EventType.PARAGRAPH_REVERTED
                                              if new_text is None else EventType.PARAGRAPH_EDITED))

            cur.execute(f"""
                SELECT edited_text FROM {config.PGSCHEMA}.review_feedback
                WHERE pair_id = %s AND paragraph_idx = %s
            """, (pair_id, paragraph_idx))
            prev_row = cur.fetchone()
            previous_text = prev_row[0] if prev_row else None

            if previous_text == new_text:
                return {"paragraph_idx": paragraph_idx, "edited_text": new_text,
                        "changed": False}

            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.review_feedback
                    (pair_id, paragraph_idx, status, comment, reviewer, updated_at, edited_text)
                VALUES (%s, %s, 'pending', NULL, %s, now(), %s)
                ON CONFLICT (pair_id, paragraph_idx) DO UPDATE SET
                    edited_text = EXCLUDED.edited_text,
                    reviewer    = EXCLUDED.reviewer,
                    updated_at  = now()
                RETURNING paragraph_idx, status, comment, reviewer, updated_at, edited_text
            """, (pair_id, paragraph_idx, reviewer, new_text))
            cols = [d.name for d in cur.description]
            row = cur.fetchone()

            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.review_edit_history
                    (pair_id, paragraph_idx, previous_text, new_text, reviewer)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING edit_id
            """, (pair_id, paragraph_idx, previous_text, new_text, reviewer))
            edit_id = cur.fetchone()[0]

            _emit_audit(cur, pair_id=pair_id,
                        event_type=(EventType.PARAGRAPH_REVERTED if new_text is None
                                    else EventType.PARAGRAPH_EDITED),
                        actor=reviewer, paragraph_idx=paragraph_idx,
                        before={"text": previous_text},
                        after={"text": new_text},
                        correlation_id=f"edit:{edit_id}")
        conn.commit()
    out = dict(zip(cols, row))
    out["changed"] = True
    return out


def list_edit_history(pair_id: str, limit: int = 200) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT edit_id, paragraph_idx, previous_text, new_text, reviewer, edited_at
                FROM {config.PGSCHEMA}.review_edit_history
                WHERE pair_id = %s
                ORDER BY edited_at DESC, edit_id DESC
                LIMIT %s
            """, (pair_id, limit))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reviewed-DOCX publish (legacy: writes a reviewed copy, not the gold promotion)
# ---------------------------------------------------------------------------


def record_publish(pair_id: str, output_path: str, edits_applied: int,
                   published_by: str) -> dict:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _assert_unlocked(cur, pair_id, actor=published_by,
                             attempted_event=EventType.REVIEWED_DOCX_PUBLISHED)
            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.review_publish_log
                    (pair_id, output_path, edits_applied, published_by)
                VALUES (%s, %s, %s, %s)
                RETURNING publish_id, pair_id, output_path, edits_applied, published_by, published_at
            """, (pair_id, output_path, edits_applied, published_by))
            cols = [d.name for d in cur.description]
            row = cur.fetchone()
            publish_id = row[0]
            # Baseline: every paragraph's current `edited_text` is now baked
            # into the published reviewed DOCX, so it stops being "pending".
            # The reviewer can still see the overlay (we don't NULL edited_text);
            # they just won't see a "1 edit pending" counter, and Promote-to-Gold
            # becomes available once certification reaches 100%.
            cur.execute(f"""
                UPDATE {config.PGSCHEMA}.review_feedback
                SET last_published_text = edited_text
                WHERE pair_id = %s
            """, (pair_id,))
            _emit_audit(cur, pair_id=pair_id,
                        event_type=EventType.REVIEWED_DOCX_PUBLISHED,
                        actor=published_by,
                        after={"output_path": output_path,
                               "edits_applied": edits_applied,
                               "publish_id": publish_id},
                        correlation_id=f"publish:{publish_id}")
        conn.commit()
    return dict(zip(cols, row))


def count_pending_edits(pair_id: str) -> int:
    """Pending = edits not yet baked into a published reviewed DOCX.
    A paragraph counts when its live `edited_text` differs from the
    `last_published_text` snapshot (NULL-safe). Excludes paragraphs that
    have never had any edit (both NULL)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*)
                FROM {config.PGSCHEMA}.review_feedback
                WHERE pair_id = %s
                  AND edited_text IS DISTINCT FROM last_published_text
                  AND (edited_text IS NOT NULL OR last_published_text IS NOT NULL)
            """, (pair_id,))
            return int(cur.fetchone()[0] or 0)


def get_pending_edits(pair_id: str) -> dict[int, str]:
    """Map of paragraph_idx → current edited_text for paragraphs whose live edit
    hasn't been baked into a publish yet. Used by the Publish dialog so it
    only writes the things that actually changed since the last publish."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT paragraph_idx, edited_text
                FROM {config.PGSCHEMA}.review_feedback
                WHERE pair_id = %s
                  AND edited_text IS NOT NULL
                  AND edited_text IS DISTINCT FROM last_published_text
            """, (pair_id,))
            return {r[0]: r[1] for r in cur.fetchall()}


def list_publish_log(pair_id: str) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT publish_id, output_path, edits_applied, published_by, published_at
                FROM {config.PGSCHEMA}.review_publish_log
                WHERE pair_id = %s
                ORDER BY published_at DESC, publish_id DESC
            """, (pair_id,))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def next_publish_version(pair_id: str) -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) FROM {config.PGSCHEMA}.review_publish_log
                WHERE pair_id = %s
            """, (pair_id,))
            return int(cur.fetchone()[0]) + 1


# ---------------------------------------------------------------------------
# Golden promotion — the compliance lock-in moment
# ---------------------------------------------------------------------------


def is_ready_for_gold(pair_id: str) -> tuple[bool, str]:
    """Returns (ready, reason). Ready iff:
      * lifecycle = UNDER_REVIEW
      * total_paragraphs known and > 0
      * certified count == total
      * zero flagged
      * zero pending edits (every edited_text is null OR has been published
        via the reviewed-DOCX writeback — but for gold we want the source
        of truth to be edits-free; reviewer must publish edits first)
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    p.lifecycle_state, p.total_paragraphs,
                    COUNT(f.*) FILTER (WHERE f.status = 'certified') AS certified,
                    COUNT(f.*) FILTER (WHERE f.status = 'flagged')   AS flagged,
                    COUNT(f.*) FILTER (
                        WHERE f.edited_text IS DISTINCT FROM f.last_published_text
                          AND (f.edited_text IS NOT NULL OR f.last_published_text IS NOT NULL)
                    ) AS pending_edits
                FROM {config.PGSCHEMA}.review_pairs p
                LEFT JOIN {config.PGSCHEMA}.review_feedback f ON f.pair_id = p.pair_id
                WHERE p.pair_id = %s
                GROUP BY p.pair_id
            """, (pair_id,))
            row = cur.fetchone()
            if not row:
                return False, "pair not found"
            state, total, certified, flagged, pending_edits = row
            if state != "UNDER_REVIEW":
                return False, f"document is in state {state}, not UNDER_REVIEW"
            if not total:
                return False, "no paragraphs yet (document not rendered)"
            if certified < total:
                return False, f"only {certified}/{total} paragraphs certified"
            if flagged:
                return False, f"{flagged} paragraph(s) still flagged"
            if pending_edits:
                return False, f"{pending_edits} paragraph(s) have unpublished edits — publish a reviewed DOCX first"
            return True, "ready"


def begin_gold_promotion(pair_id: str, actor: str) -> None:
    """UNDER_REVIEW → PROMOTING. Audit-only state change; the file copy hasn't
    happened yet. If the copy fails the caller should call abort_gold_promotion."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT lifecycle_state FROM {config.PGSCHEMA}.review_pairs WHERE pair_id = %s",
                (pair_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"pair {pair_id} not found")
            current = row[0]
            if current != "UNDER_REVIEW":
                raise RuntimeError(f"cannot start promotion from state {current}")
            cur.execute(
                f"UPDATE {config.PGSCHEMA}.review_pairs SET lifecycle_state = 'PROMOTING' WHERE pair_id = %s",
                (pair_id,),
            )
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.GOLD_PROMOTION_STARTED,
                        actor=actor, before={"state": current},
                        after={"state": "PROMOTING"})
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.LIFECYCLE_TRANSITIONED,
                        actor=actor, before={"state": current},
                        after={"state": "PROMOTING"})
        conn.commit()


def abort_gold_promotion(pair_id: str, actor: str, reason: str) -> None:
    """PROMOTING → UNDER_REVIEW (rollback path)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {config.PGSCHEMA}.review_pairs SET lifecycle_state = 'UNDER_REVIEW' "
                f"WHERE pair_id = %s AND lifecycle_state = 'PROMOTING'",
                (pair_id,),
            )
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.GOLD_PROMOTION_FAILED,
                        actor=actor, after={"reason": reason, "state": "UNDER_REVIEW"})
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.LIFECYCLE_TRANSITIONED,
                        actor=actor, before={"state": "PROMOTING"},
                        after={"state": "UNDER_REVIEW"})
        conn.commit()


def complete_gold_promotion(
    *,
    pair_id: str,
    actor: str,
    golden_original_path: str,
    golden_translated_path: str,
    golden_original_hash: str,
    golden_translated_hash: str,
    total_paragraphs: int,
    certified_paragraphs: int,
    edits_applied: int,
    distinct_reviewers: list[str],
) -> dict:
    """PROMOTING → PUBLISHED. Writes golden_publications row, sets locked_at,
    and emits the GOLD_PROMOTED event. All in one transaction so a crash
    leaves the doc either PROMOTING (re-runnable) or fully PUBLISHED."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {config.PGSCHEMA}.golden_publications
                    (pair_id, golden_original_path, golden_translated_path,
                     golden_original_hash, golden_translated_hash,
                     total_paragraphs, certified_paragraphs, edits_applied,
                     distinct_reviewers, published_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING publication_id, published_at
            """, (
                pair_id, golden_original_path, golden_translated_path,
                golden_original_hash, golden_translated_hash,
                total_paragraphs, certified_paragraphs, edits_applied,
                json.dumps(sorted(set(distinct_reviewers))),
                actor,
            ))
            publication_id, published_at = cur.fetchone()
            cur.execute(f"""
                UPDATE {config.PGSCHEMA}.review_pairs
                SET lifecycle_state = 'PUBLISHED',
                    locked_at       = now(),
                    finalized_at    = now()
                WHERE pair_id = %s AND lifecycle_state = 'PROMOTING'
            """, (pair_id,))
            if cur.rowcount != 1:
                # Someone else promoted concurrently or state drifted; bail.
                raise RuntimeError(f"could not lock {pair_id}: state changed under us")

            audit_after = {
                "publication_id": publication_id,
                "golden_original_path": golden_original_path,
                "golden_translated_path": golden_translated_path,
                "golden_original_hash": golden_original_hash,
                "golden_translated_hash": golden_translated_hash,
                "total_paragraphs": total_paragraphs,
                "certified_paragraphs": certified_paragraphs,
                "edits_applied": edits_applied,
                "distinct_reviewers": sorted(set(distinct_reviewers)),
            }
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.GOLD_PROMOTED,
                        actor=actor, after=audit_after,
                        correlation_id=f"publication:{publication_id}")
            _emit_audit(cur, pair_id=pair_id, event_type=EventType.LIFECYCLE_TRANSITIONED,
                        actor=actor, before={"state": "PROMOTING"},
                        after={"state": "PUBLISHED"})
        conn.commit()
    return {
        "publication_id": publication_id,
        "published_at":   published_at,
        "pair_id":        pair_id,
    }


def get_publication(pair_id: str) -> dict | None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT publication_id, pair_id,
                       golden_original_path, golden_translated_path,
                       golden_original_hash, golden_translated_hash,
                       total_paragraphs, certified_paragraphs, edits_applied,
                       distinct_reviewers, published_by, published_at,
                       delta_synced_at
                FROM {config.PGSCHEMA}.golden_publications
                WHERE pair_id = %s
            """, (pair_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Audit query
# ---------------------------------------------------------------------------


def list_audit_events(pair_id: str, limit: int = 500) -> list[dict]:
    """Reverse-chronological audit trail for a single document. Used by the
    in-app audit drawer and is the foundation for compliance reports."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT event_id, event_type, actor, actor_type, paragraph_idx,
                       before_value, after_value, correlation_id, event_at
                FROM {config.PGSCHEMA}.audit_events
                WHERE pair_id = %s
                ORDER BY event_at DESC, event_id DESC
                LIMIT %s
            """, (pair_id, limit))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def bulk_upsert_confidence(pair_id: str, rows: list[dict]) -> int:
    """Insert per-paragraph confidence scores. `rows` is a list of dicts with
    keys: paragraph_idx, length_ratio, untranslated_pct, repeated_ngrams,
    confidence, source_lang, target_lang.

    Idempotent — re-running just overwrites with the new scores. The app
    calls this lazily on first render of a pair if confidence is missing."""
    if not rows:
        return 0
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            values_sql = ",".join(
                "(%s,%s,%s,%s,%s,%s,now(),%s,%s)" for _ in rows
            )
            params: list = []
            for r in rows:
                params.extend([
                    pair_id, r["paragraph_idx"],
                    r.get("length_ratio"), r.get("untranslated_pct"),
                    r.get("repeated_ngrams"), r["confidence"],
                    r.get("source_lang"), r.get("target_lang"),
                ])
            cur.execute(f"""
                INSERT INTO {s}.paragraph_confidence
                    (pair_id, paragraph_idx, length_ratio, untranslated_pct,
                     repeated_ngrams, confidence, computed_at, source_lang, target_lang)
                VALUES {values_sql}
                ON CONFLICT (pair_id, paragraph_idx) DO UPDATE SET
                    length_ratio     = EXCLUDED.length_ratio,
                    untranslated_pct = EXCLUDED.untranslated_pct,
                    repeated_ngrams  = EXCLUDED.repeated_ngrams,
                    confidence       = EXCLUDED.confidence,
                    computed_at      = EXCLUDED.computed_at,
                    source_lang      = EXCLUDED.source_lang,
                    target_lang      = EXCLUDED.target_lang
            """, params)
            count = cur.rowcount
        conn.commit()
    return count


def get_confidence(pair_id: str) -> dict[int, dict]:
    """Map of paragraph_idx → {length_ratio, untranslated_pct, repeated_ngrams, confidence}.
    Empty dict if no scores yet (caller computes + upserts)."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT paragraph_idx, length_ratio, untranslated_pct,
                       repeated_ngrams, confidence
                FROM {s}.paragraph_confidence
                WHERE pair_id = %s
            """, (pair_id,))
            return {
                int(r[0]): {
                    "length_ratio":     r[1],
                    "untranslated_pct": r[2],
                    "repeated_ngrams":  r[3],
                    "confidence":       float(r[4]),
                }
                for r in cur.fetchall()
            }


def get_distinct_reviewers(pair_id: str) -> list[str]:
    """Union of every human actor who touched this document. Used at promotion
    time to populate `golden_publications.distinct_reviewers`."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT actor FROM {config.PGSCHEMA}.audit_events
                WHERE pair_id = %s AND actor_type = 'human'
            """, (pair_id,))
            return sorted(r[0] for r in cur.fetchall())


def delete_pair_state(pair_id: str) -> dict[str, int]:
    """Drop all Lakebase review state for `pair_id`. Returns rows deleted per
    table.

    Used when a document is re-uploaded under a name that already exists: the
    stale certifications/edits describe the OLD content, so carrying them over
    to new content would be worse than losing them.

    Delete order matters — `golden_publications` references review_pairs
    WITHOUT `ON DELETE CASCADE`, so it has to go first or the review_pairs
    delete raises a FK violation. review_feedback / review_edit_history /
    paragraph_confidence all cascade from review_pairs. audit_events has no FK
    (pair_id is a plain column) so it's deleted explicitly.

    NOTE: this does not touch the Delta mirror — that archive is intentionally
    append-only and is the compliance record."""
    s = config.PGSCHEMA
    counts: dict[str, int] = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for tbl in ("golden_publications", "review_publish_log",
                        "audit_events", "review_pairs"):
                cur.execute(f"DELETE FROM {s}.{tbl} WHERE pair_id = %s", (pair_id,))
                counts[tbl] = cur.rowcount
        conn.commit()
    return counts
