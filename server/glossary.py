"""Glossary of terminology entries that steer future translations.

Two kinds of entry share the `translation_glossary` table, distinguished by
the `source` column:

* ``tenant`` — mined here from `review_edit_history`. When reviewers across
  documents repeatedly correct the SAME short translated phrase to the SAME
  different phrase, that's a systematic model blind spot. Both `model_phrase`
  and `correction` are TARGET-language text. Injected into prompts as
  "preferred terminology" corrections (they can't be matched against the
  source text — we never saw the source phrase, only the bad output).

* ``seed`` / ``customer`` — bilingual pairs. `model_phrase` is SOURCE-language
  text, `correction` is the required target-language term. `seed` ships with
  the bundle (optional, public ICH-derived clinical terms); `customer` comes
  from a CSV the customer imports. Matched against the source paragraph at
  translation time (Aho-Corasick) and injected as hard translation rules.

We only mine SHORT edits (≤ 200 chars). Longer paragraphs are too varied
to glossarize — their corrections are document-specific.

The translation notebook reads this table via its Delta mirror (see
`delta_sync.sync_glossary_to_delta`), not from Lakebase directly.
"""
from __future__ import annotations
import csv
import io
from . import config
from .db import pool


# Mining thresholds — tunable. Defaults err on the side of inclusion since the
# glossary table has an `approved` flag for manual gating later if needed.
MIN_OCCURRENCES        = 2     # show up at least twice across history
MIN_DISTINCT_REVIEWERS = 1     # at least 1 distinct reviewer (can bump to 2 later)
MAX_PHRASE_LEN         = 200   # ignore long paragraph edits
MAX_ENTRIES            = 200   # cap to keep prompt-injection budget sane


def mine_glossary(
    *,
    source_lang: str | None = None,
    target_lang: str | None = None,
) -> int:
    """Scan review_edit_history → upsert into translation_glossary. Returns
    the number of distinct (model_phrase, correction) pairs touched.

    Idempotent: re-running just updates `occurrences`, `distinct_reviewers`,
    and `last_seen_at` for existing rows."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Aggregate edit history. We treat the EDIT itself as the unit —
            # `previous_text` is what the model (or a prior reviewer) had,
            # `new_text` is what the current reviewer wrote.
            #
            # Only count edits where:
            #   * neither text is NULL (skip reverts)
            #   * texts are not equal
            #   * both texts are short enough to be glossary-worthy
            cur.execute(f"""
                WITH eligible AS (
                    SELECT
                        h.previous_text AS model_phrase,
                        h.new_text      AS correction,
                        h.reviewer,
                        h.edited_at,
                        p.source_lang,
                        p.target_lang
                    FROM {s}.review_edit_history h
                    JOIN {s}.review_pairs p ON p.pair_id = h.pair_id
                    WHERE h.previous_text IS NOT NULL
                      AND h.new_text      IS NOT NULL
                      AND h.previous_text <> h.new_text
                      AND length(h.previous_text) <= {MAX_PHRASE_LEN}
                      AND length(h.new_text)      <= {MAX_PHRASE_LEN}
                      AND length(trim(h.previous_text)) > 0
                      AND length(trim(h.new_text))      > 0
                ),
                agg AS (
                    SELECT
                        source_lang,
                        target_lang,
                        model_phrase,
                        correction,
                        COUNT(*)::int                   AS occurrences,
                        COUNT(DISTINCT reviewer)::int   AS distinct_reviewers,
                        MAX(edited_at)                  AS last_seen_at
                    FROM eligible
                    GROUP BY source_lang, target_lang, model_phrase, correction
                    HAVING COUNT(*) >= %s
                       AND COUNT(DISTINCT reviewer) >= %s
                )
                INSERT INTO {s}.translation_glossary
                    (source_lang, target_lang, model_phrase, correction,
                     occurrences, distinct_reviewers, last_seen_at, approved, source, list_name)
                SELECT source_lang, target_lang, model_phrase, correction,
                       occurrences, distinct_reviewers, last_seen_at, TRUE, 'tenant', 'Mined from reviews'
                FROM agg
                ON CONFLICT (source_lang, target_lang, model_phrase, correction) DO UPDATE SET
                    occurrences        = EXCLUDED.occurrences,
                    distinct_reviewers = EXCLUDED.distinct_reviewers,
                    last_seen_at       = EXCLUDED.last_seen_at
                RETURNING entry_id
            """, (MIN_OCCURRENCES, MIN_DISTINCT_REVIEWERS))
            rows = cur.fetchall()
        conn.commit()
    return len(rows)


def list_glossary(
    *,
    source_lang: str | None = None,
    target_lang: str | None = None,
    approved_only: bool = True,
    source: str | None = None,
    list_name: str | None = None,
    limit: int = MAX_ENTRIES,
) -> list[dict]:
    """Return glossary entries. Sorted by occurrences DESC so the most
    confident/frequent corrections come first — useful when truncating to fit
    a prompt-injection budget. `source` filters to 'tenant'/'seed'/'customer';
    `list_name` filters to one named list."""
    s = config.PGSCHEMA
    clauses: list[str] = []
    params: list = []
    if source_lang is not None:
        clauses.append("source_lang = %s"); params.append(source_lang)
    if target_lang is not None:
        clauses.append("target_lang = %s"); params.append(target_lang)
    if source is not None:
        clauses.append("source = %s"); params.append(source)
    if list_name is not None:
        clauses.append("list_name = %s"); params.append(list_name)
    if approved_only:
        clauses.append("approved = TRUE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT entry_id, source_lang, target_lang,
                       model_phrase, correction,
                       occurrences, distinct_reviewers,
                       last_seen_at, created_at, approved, source, list_name
                FROM {s}.translation_glossary
                {where}
                ORDER BY occurrences DESC, last_seen_at DESC
                LIMIT %s
            """, [*params, limit])
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


# Default list names per source, when the caller doesn't supply one.
_DEFAULT_LIST = {"seed": "Seed — ICH clinical", "tenant": "Mined from reviews"}


def ingest_glossary_rows(
    rows: list[dict],
    *,
    source: str = "customer",
    approved: bool = True,
    list_name: str | None = None,
) -> int:
    """Upsert bilingual glossary pairs into a named list. Each row needs
    source_lang, target_lang, model_phrase (source-language phrase), correction
    (required target-language term). Returns rows touched.

    Used for both the shipped seed file (source='seed') and customer CSV
    imports (source='customer'). On conflict with an existing entry the original
    `source` and `list_name` are kept — a customer import never silently demotes
    a mined tenant entry or re-homes an existing term into a different list; only
    brand-new phrases land in the given list."""
    s = config.PGSCHEMA
    lname = (list_name or "").strip() or _DEFAULT_LIST.get(source, "Imported")
    clean: list[tuple] = []
    for r in rows:
        sl = (r.get("source_lang") or "").strip().lower()
        tl = (r.get("target_lang") or "").strip().lower()
        mp = (r.get("model_phrase") or r.get("source_phrase") or "").strip()
        co = (r.get("correction") or r.get("target_phrase") or "").strip()
        if not (sl and tl and mp and co) or len(mp) > MAX_PHRASE_LEN or len(co) > MAX_PHRASE_LEN:
            continue
        clean.append((sl, tl, mp, co))
    if not clean:
        return 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(f"""
                INSERT INTO {s}.translation_glossary
                    (source_lang, target_lang, model_phrase, correction,
                     occurrences, distinct_reviewers, approved, source, list_name)
                VALUES (%s, %s, %s, %s, 1, 1, %s, %s, %s)
                ON CONFLICT (source_lang, target_lang, model_phrase, correction) DO UPDATE SET
                    last_seen_at = now(),
                    approved     = EXCLUDED.approved
            """, [(sl, tl, mp, co, approved, source, lname) for sl, tl, mp, co in clean])
        conn.commit()
    return len(clean)


def ingest_glossary_csv(
    data: bytes | str,
    *,
    source: str = "customer",
    approved: bool = True,
    list_name: str | None = None,
) -> int:
    """Parse a glossary CSV and upsert its rows into a named list. Expected
    header (order-free): source_lang, target_lang, model_phrase (or
    source_phrase), correction (or target_phrase). Extra columns are ignored."""
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    return ingest_glossary_rows(list(reader), source=source, approved=approved, list_name=list_name)


def toggle_approval(entry_id: int, approved: bool) -> None:
    """Manual override for any glossary entry."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {s}.translation_glossary SET approved = %s WHERE entry_id = %s",
                (approved, entry_id),
            )
        conn.commit()


def set_approval_batch(*, entry_ids: list[int] | None = None,
                       list_name: str | None = None, approved: bool) -> int:
    """Approve/unapprove many entries at once — by explicit ids or by list.
    Returns the number of rows updated."""
    s = config.PGSCHEMA
    if list_name is not None:
        where, param = "list_name = %s", list_name
    elif entry_ids:
        where, param = "entry_id = ANY(%s)", list(entry_ids)
    else:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {s}.translation_glossary SET approved = %s WHERE {where}",
                (approved, param),
            )
            n = cur.rowcount
        conn.commit()
    return n


def delete_list(list_name: str) -> int:
    """Delete an entire named list (all its entries). Returns rows removed."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {s}.translation_glossary WHERE list_name = %s", (list_name,)
            )
            n = cur.rowcount
        conn.commit()
    return n


def rename_list(old_name: str, new_name: str) -> int:
    """Rename a named list. Returns rows touched."""
    s = config.PGSCHEMA
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("new list name is required")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {s}.translation_glossary SET list_name = %s WHERE list_name = %s",
                (new_name, old_name),
            )
            n = cur.rowcount
        conn.commit()
    return n


def list_summary() -> list[dict]:
    """One row per named list: name, predominant source, entry counts, and the
    distinct language pairs it spans."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COALESCE(list_name, '(unnamed)')          AS list_name,
                    MIN(source)                               AS source,
                    COUNT(*)::int                             AS total,
                    COUNT(*) FILTER (WHERE approved)::int     AS approved_count,
                    string_agg(DISTINCT source_lang, ',')     AS source_langs,
                    string_agg(DISTINCT target_lang, ',')     AS target_langs,
                    MAX(last_seen_at)                         AS last_seen_at
                FROM {s}.translation_glossary
                GROUP BY COALESCE(list_name, '(unnamed)')
                ORDER BY MIN(source), list_name
            """)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def conflicting_entry_ids() -> set[int]:
    """Entry ids that conflict: the SAME source phrase (source_lang, target_lang,
    model_phrase) mapped to more than one distinct correction among APPROVED
    entries — the injector would feed the model contradictory rules. Surfaced in
    the UI so the reviewer can disable the loser."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                WITH conf AS (
                    SELECT source_lang, target_lang, model_phrase
                    FROM {s}.translation_glossary
                    WHERE approved
                    GROUP BY source_lang, target_lang, model_phrase
                    HAVING COUNT(DISTINCT correction) > 1
                )
                SELECT g.entry_id
                FROM {s}.translation_glossary g
                JOIN conf c
                  ON c.source_lang = g.source_lang
                 AND c.target_lang = g.target_lang
                 AND c.model_phrase = g.model_phrase
                WHERE g.approved
            """)
            return {r[0] for r in cur.fetchall()}


def glossary_for_prompt(
    *,
    source_lang: str,
    target_lang: str,
    top_n: int = 50,
) -> list[tuple[str, str]]:
    """Top-N (model_phrase, correction) pairs for injection into a translation
    prompt. Returns an empty list when nothing's been mined yet — caller can
    no-op. NOTE: the production read path is the Delta mirror (the translation
    job can't reach Lakebase); this helper serves the app's own preview UI."""
    entries = list_glossary(
        source_lang=source_lang, target_lang=target_lang,
        approved_only=True, limit=top_n,
    )
    return [(e["model_phrase"], e["correction"]) for e in entries]
