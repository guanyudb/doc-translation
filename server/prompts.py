"""Translation prompts — the system prompt the model receives per document.

Customers manage a library of named prompts here (full CRUD). At upload time
the reviewer picks one; its full text is frozen with the document (a `.prompt`
sidecar written next to the raw `.docx`) so editing or deleting a prompt later
never changes what a past document was translated with. See
`server_api.upload_document` (writes the sidecar) and
`setup/auto_translate_watcher.py:_prompt_for` (reads it back in the pipeline).

Unlike the glossary, prompts are NOT mirrored to Delta: because the chosen text
is snapshotted at upload, the translation notebook receives it directly as a job
argument and never needs to read this table.

Every mutation appends an `audit_events` row (with `pair_id = NULL` — prompts are
not document-scoped) inside the same transaction, matching `store.py`'s posture.
"""
from __future__ import annotations
from . import config
from .db import pool
from . import store


# Seed + editor template. Mirrors the notebook's built-in fallback prompt
# (setup/docx_inplace_translation.py `TRANSLATE_SYSTEM`) — keep the two in sync.
# The `{lang}` token is substituted with the document's target language at
# translation time via str.replace (so stray braces in a custom prompt are safe).
DEFAULT_PROMPT_BODY = (
    "You are a professional medical and clinical document translator working on FDA "
    "regulatory submissions. Translate the user's text to {lang}.\n"
    "STRICT RULES:\n"
    "1. Return ONLY the translated text. No commentary, no quotes, no labels, no explanations.\n"
    "2. Preserve numbers, units, percentages, dates, dosages, and proper nouns exactly.\n"
    "3. Preserve URLs, email addresses, file paths, and code-like tokens unchanged.\n"
    "4. Keep medical terminology accurate and consistent.\n"
    "5. If the input is already in {lang}, return it unchanged.\n"
    "6. If the input is empty, whitespace, or only punctuation/numbers, return it unchanged.\n"
    "7. Do not add or remove leading/trailing whitespace beyond what the source has."
)

DEFAULT_PROMPT_NAME = "Medical / clinical (default)"

MAX_NAME_LEN = 200
# The chosen prompt is frozen into a job argument passed to the translation
# notebook; keep it comfortably under the notebook-parameter length limit.
MAX_BODY_LEN = 8000

_COLS = ("prompt_id, name, body, description, "
         "created_by, created_at, updated_by, updated_at")


class DuplicateNameError(ValueError):
    """A prompt with the requested name already exists (name is UNIQUE)."""


def _rows(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _validate(name: str, body: str) -> tuple[str, str]:
    name = (name or "").strip()
    body = (body or "").strip()
    if not name:
        raise ValueError("name is required")
    if not body:
        raise ValueError("body is required")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name too long (max {MAX_NAME_LEN} chars)")
    if len(body) > MAX_BODY_LEN:
        raise ValueError(f"body too long (max {MAX_BODY_LEN} chars)")
    return name, body


def list_prompts() -> list[dict]:
    """All prompts, alphabetical by name."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM {s}.translation_prompts ORDER BY name ASC")
            return _rows(cur)


def get_prompt(prompt_id: int) -> dict | None:
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLS} FROM {s}.translation_prompts WHERE prompt_id = %s",
                (prompt_id,),
            )
            rows = _rows(cur)
    return rows[0] if rows else None


def create_prompt(*, name: str, body: str,
                  description: str | None = None, actor: str = "system") -> dict:
    name, body = _validate(name, body)
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(f"""
                    INSERT INTO {s}.translation_prompts
                        (name, body, description, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING {_COLS}
                """, (name, body, (description or None), actor, actor))
            except Exception as ex:  # pragma: no cover - surfaced as 409 by the route
                if _is_unique_violation(ex):
                    raise DuplicateNameError(name) from ex
                raise
            row = _rows(cur)[0]
            store._emit_audit(
                cur, pair_id=None, event_type=store.EventType.PROMPT_CREATED,
                actor=actor, after={"prompt_id": row["prompt_id"], "name": name},
            )
        conn.commit()
    return row


def update_prompt(prompt_id: int, *, name: str, body: str,
                  description: str | None = None, actor: str = "system") -> dict | None:
    name, body = _validate(name, body)
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT name FROM {s}.translation_prompts WHERE prompt_id = %s",
                (prompt_id,),
            )
            prev = cur.fetchone()
            if not prev:
                return None
            try:
                cur.execute(f"""
                    UPDATE {s}.translation_prompts
                    SET name = %s, body = %s, description = %s,
                        updated_by = %s, updated_at = now()
                    WHERE prompt_id = %s
                    RETURNING {_COLS}
                """, (name, body, (description or None), actor, prompt_id))
            except Exception as ex:
                if _is_unique_violation(ex):
                    raise DuplicateNameError(name) from ex
                raise
            row = _rows(cur)[0]
            store._emit_audit(
                cur, pair_id=None, event_type=store.EventType.PROMPT_UPDATED,
                actor=actor, before={"name": prev[0]},
                after={"prompt_id": prompt_id, "name": name},
            )
        conn.commit()
    return row


def delete_prompt(prompt_id: int, *, actor: str = "system") -> bool:
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {s}.translation_prompts WHERE prompt_id = %s RETURNING name",
                (prompt_id,),
            )
            r = cur.fetchone()
            if not r:
                return False
            store._emit_audit(
                cur, pair_id=None, event_type=store.EventType.PROMPT_DELETED,
                actor=actor, before={"prompt_id": prompt_id, "name": r[0]},
            )
        conn.commit()
    return True


def clone_prompt(prompt_id: int, *, new_name: str | None = None,
                 actor: str = "system") -> dict | None:
    src = get_prompt(prompt_id)
    if src is None:
        return None
    name, body = _validate(new_name or f"{src['name']} (copy)", src["body"])
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(f"""
                    INSERT INTO {s}.translation_prompts
                        (name, body, description, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING {_COLS}
                """, (name, body, src.get("description"), actor, actor))
            except Exception as ex:
                if _is_unique_violation(ex):
                    raise DuplicateNameError(name) from ex
                raise
            row = _rows(cur)[0]
            store._emit_audit(
                cur, pair_id=None, event_type=store.EventType.PROMPT_CLONED,
                actor=actor,
                before={"cloned_from": prompt_id, "name": src["name"]},
                after={"prompt_id": row["prompt_id"], "name": name},
            )
        conn.commit()
    return row


def seed_default_prompt() -> bool:
    """Insert the built-in default prompt if the table has no rows yet.
    Idempotent — safe to call on every app startup. Returns True if it inserted.
    Requires only INSERT (the app SP has it), so the app can self-seed once the
    table exists (created by postdeploy or the schema-migration one-liner)."""
    s = config.PGSCHEMA
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {s}.translation_prompts LIMIT 1")
            if cur.fetchone():
                return False
            cur.execute(f"""
                INSERT INTO {s}.translation_prompts
                    (name, body, description, created_by, updated_by)
                VALUES (%s, %s, %s, 'system', 'system')
                ON CONFLICT (name) DO NOTHING
            """, (
                DEFAULT_PROMPT_NAME, DEFAULT_PROMPT_BODY,
                "Built-in FDA / clinical translation prompt. Seeded automatically.",
            ))
        conn.commit()
    return True


def _is_unique_violation(ex: Exception) -> bool:
    # psycopg raises UniqueViolation with sqlstate 23505; match defensively so we
    # don't hard-depend on the psycopg.errors import path.
    return getattr(ex, "sqlstate", None) == "23505" or "unique" in str(ex).lower()
