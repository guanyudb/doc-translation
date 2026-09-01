"""App-level runtime settings — a single JSON document in the UC Volume.

One source of truth for the settings the first-run Setup page manages: the
translation model endpoint, the default target language, and branding
(title / logo). Stored at ``{VOLUME_ROOT}/config/settings.json`` so BOTH sides
can reach it — the app (this module) reads + writes it; the translation
pipeline (`setup/auto_translate_watcher.py`) reads the model endpoint at job
start. It deliberately does NOT live in Lakebase: the pipeline reaches the
Volume + Delta, not Lakebase, and the Volume is the shared surface both sides
already use for documents and per-file sidecars.

Precedence: a value saved through the app wins; otherwise the deploy-time
default (env / bundle var) applies. `is_configured` flips to true the first
time settings are saved, which the frontend uses to gate the first-run wizard.
"""
from __future__ import annotations

import datetime
import json
import os

from . import config, volume

SETTINGS_PATH = f"{config.VOLUME_ROOT}/config/settings.json"

# Keys the app owns + persists. Anything else in the stored JSON is ignored on
# load (forward/backward compatible) and never written.
_KEYS = ("model_endpoint", "target_language", "app_title", "logo_url", "logo_alt")

# Simple module cache. Branding is read on every /api/config, and the Volume
# Files API is not free — cache in memory and refresh on save. Single-instance
# app, so cross-replica staleness isn't a concern here.
_cache: dict | None = None


def _defaults() -> dict:
    """Deploy-time defaults from env / bundle vars, used until the app is
    configured (or for any field the saved settings omit)."""
    return {
        "model_endpoint": os.environ.get("TRANSLATION_MODEL_ENDPOINT", "databricks-claude-sonnet-4-6"),
        "target_language": os.environ.get("TRANSLATION_TARGET_LANGUAGE", "English"),
        "app_title": config.APP_TITLE,
        "logo_url": config.APP_LOGO_URL,
        "logo_alt": config.APP_LOGO_ALT,
        "is_configured": False,
        "updated_at": None,
        "updated_by": None,
    }


def load(force: bool = False) -> dict:
    """Current settings = defaults overlaid with whatever's persisted in the
    Volume. Cached; pass force=True to re-read. Never raises — a missing/broken
    settings file just yields defaults (is_configured=False → show setup)."""
    global _cache
    if _cache is not None and not force:
        return _cache
    merged = _defaults()
    raw = None
    try:
        raw = volume.read_text(SETTINGS_PATH)
    except Exception:
        raw = None
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                for k in (*_KEYS, "is_configured", "updated_at", "updated_by"):
                    if k in stored and stored[k] is not None:
                        merged[k] = stored[k]
        except Exception:
            pass  # corrupt file → fall back to defaults
    _cache = merged
    return merged


def save(patch: dict, actor: str = "system") -> dict:
    """Apply a partial update, mark the app configured, persist to the Volume,
    and refresh the cache. Returns the full new settings dict."""
    cur = load(force=True)
    for k in _KEYS:
        if k in patch and patch[k] is not None:
            cur[k] = str(patch[k]).strip() if isinstance(patch[k], str) else patch[k]
    # Blank branding strings mean "use the default" — normalize to None so the
    # frontend falls back to the icon/title rather than rendering an empty logo.
    for k in ("logo_url", "logo_alt"):
        if isinstance(cur.get(k), str) and not cur[k].strip():
            cur[k] = None
    cur["is_configured"] = True
    cur["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur["updated_by"] = actor

    volume.upload_docx(SETTINGS_PATH, json.dumps(cur, ensure_ascii=False).encode("utf-8"))

    global _cache
    _cache = cur

    # Best-effort audit — settings changes are operationally significant, but
    # never block the save on the audit write.
    try:
        from .db import pool
        with pool.connection() as conn:
            with conn.cursor() as c:
                store_emit = getattr(__import__("server.store", fromlist=["_emit_audit"]), "_emit_audit")
                store_emit(c, pair_id=None, event_type="SETTINGS_UPDATED", actor=actor,
                           after={k: cur.get(k) for k in _KEYS})
            conn.commit()
    except Exception:
        pass

    return cur


def model_endpoint_from_volume(default: str) -> str:
    """Read ONLY the model endpoint straight from the Volume JSON, for the
    pipeline watcher (which has no module cache and no Lakebase). Returns
    `default` (the job param) when unset or unconfigured."""
    try:
        raw = volume.read_text(SETTINGS_PATH)
        if raw:
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("is_configured") and (d.get("model_endpoint") or "").strip():
                return d["model_endpoint"].strip()
    except Exception:
        pass
    return default
