"""Mine a glossary of (model-output → reviewer-correction) patterns from
the live `review_edit_history` table.

The premise: when reviewers across multiple documents repeatedly correct the
SAME short translated phrase to the SAME different phrase, that's a glossary
entry — the model has a systematic blind spot we can patch by injecting the
correction into future translation prompts.

We only mine SHORT edits (≤ 200 chars). Longer paragraphs are too varied
to glossarize — their corrections are document-specific.

This is Phase 1b. The mining job populates `translation_glossary`. Phase 1c
(later) reads from that table at translation time and injects the top-N
approved entries into the FMAPI system prompt.
"""
from __future__ import annotations
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
                     occurrences, distinct_reviewers, last_seen_at, approved)
                SELECT source_lang, target_lang, model_phrase, correction,
                       occurrences, distinct_reviewers, last_seen_at, TRUE
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
    limit: int = MAX_ENTRIES,
) -> list[dict]:
    """Return mined glossary entries. Sorted by occurrences DESC so the most
    confident/frequent corrections come first — useful when truncating to fit
    a prompt-injection budget."""
    s = config.PGSCHEMA
    clauses: list[str] = []
    params: list = []
    if source_lang is not None:
        clauses.append("source_lang = %s"); params.append(source_lang)
    if target_lang is not None:
        clauses.append("target_lang = %s"); params.append(target_lang)
    if approved_only:
        clauses.append("approved = TRUE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT entry_id, source_lang, target_lang,
                       model_phrase, correction,
                       occurrences, distinct_reviewers,
                       last_seen_at, created_at, approved
                FROM {s}.translation_glossary
                {where}
                ORDER BY occurrences DESC, last_seen_at DESC
                LIMIT %s
            """, [*params, limit])
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


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


def glossary_for_prompt(
    *,
    source_lang: str,
    target_lang: str,
    top_n: int = 50,
) -> list[tuple[str, str]]:
    """Top-N (model_phrase, correction) pairs for injection into a translation
    prompt. Phase 1c will wire this into the inner translation notebook.
    Returns an empty list when nothing's been mined yet — caller can no-op."""
    entries = list_glossary(
        source_lang=source_lang, target_lang=target_lang,
        approved_only=True, limit=top_n,
    )
    return [(e["model_phrase"], e["correction"]) for e in entries]
