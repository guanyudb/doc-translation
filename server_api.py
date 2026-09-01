"""Doc Translation Review — FastAPI backend serving the React SPA in static/.

The review workflow + persistence live in server/ (Lakebase Postgres store,
UC Volume IO, DOCX→HTML render, heuristic confidence, glossary mining +
injection, Delta mirror). This layer exposes them as a JSON API and serves the
built React frontend.

    /api/config                                app-level info (reviewer, target lang)
    /api/pairs                                 list reviewable document pairs
    /api/pairs/{id}                            paragraph-level review detail
    /api/pairs/{id}/preview/{side}             DOCX rendered to HTML (with edits)
    /api/pairs/{id}/paragraphs/{i}/status      certify / flag / pending
    /api/pairs/{id}/paragraphs/{i}/comment     reviewer comment
    /api/pairs/{id}/paragraphs/{i}/edit        edit / revert translated text
    /api/pairs/{id}/certify-all                bulk-certify
    /api/pairs/{id}/publish                    bake edits → versioned reviewed DOCX
    /api/pairs/{id}/promote                    freeze + copy to golden zone + Delta
    /api/pairs/{id}/audit                      append-only event log
    /api/glossary  (+ /import /mine /sync-delta /{id}/approve)   glossary admin

The Streamlit app this replaces is preserved at legacy/streamlit_app.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict

from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Path, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config, volume, store, docx_render, auth, delta_sync
from server import confidence as conf_mod
from server import glossary as glossary_mod
from server import prompts as prompts_mod
from server import settings as settings_mod
from server.db import pool

log = logging.getLogger("doc_translation")

app = FastAPI(title="Doc Translation Review")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Set by _startup() if the pool can't be opened at boot — surfaced by
# /api/health so operators can tell a broken app from a healthy one (Databricks
# Apps report RUNNING even when the backend can't reach its data layer).
_startup_error: str | None = None


@app.middleware("http")
async def _capture_identity(request: Request, call_next):
    # Databricks Apps forwards the signed-in user as X-Forwarded-Email. Stash
    # the request headers so auth.reviewer() can read them (contextvar-based,
    # so it's correct even under concurrency).
    auth.set_request_headers(dict(request.headers))
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    global _startup_error
    # Open the psycopg pool once. Safe if already open (idempotent proxy).
    # Never block boot on it — but DO record the failure so /api/health can
    # report it (Databricks Apps show RUNNING even when the DB is unreachable).
    try:
        pool.open(wait=True, timeout=30.0)
    except Exception as e:
        _startup_error = f"pool.open failed: {e}"
        log.exception("startup: could not open Lakebase pool")
    # Seed the built-in default prompt if the library is empty. Idempotent; the
    # app SP has INSERT so it can self-seed once the table exists. Best-effort.
    try:
        prompts_mod.seed_default_prompt()
    except Exception:
        log.exception("startup: default prompt seed failed")


@app.get("/api/health")
def health():
    """Readiness probe. Databricks Apps report RUNNING even when the backend
    can't reach Lakebase — this tells them apart. 503 when the pool never
    opened or a probe query fails."""
    detail: dict = {"startup_error": _startup_error}
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        detail["lakebase"] = "ok"
    except Exception as e:
        detail["lakebase"] = f"error: {e}"
        return JSONResponse(status_code=503, content={"status": "unhealthy", **detail})
    if _startup_error:
        return JSONResponse(status_code=503, content={"status": "degraded", **detail})
    return {"status": "ok", **detail}


# ---------------------------------------------------------------------------
# Pair resolution + rendering (cached-ish helpers)
# ---------------------------------------------------------------------------

# Render is relatively expensive (mammoth conversion); cache per (path, size).
# Bounded LRU so a long-running instance reviewing many documents doesn't grow
# memory without limit (each entry is a full rendered doc + paragraph list).
_RENDER_CACHE_MAX = 64
_render_cache: "OrderedDict[str, tuple[str, list[dict]]]" = OrderedDict()


def _render(path: str) -> tuple[str, list[dict]]:
    b = volume.read_docx(path)
    key = f"{path}:{len(b)}"
    hit = _render_cache.get(key)
    if hit is not None:
        _render_cache.move_to_end(key)  # mark most-recently-used
        return hit
    out = docx_render.render(b)
    _render_cache[key] = out
    if len(_render_cache) > _RENDER_CACHE_MAX:
        _render_cache.popitem(last=False)  # evict least-recently-used
    return out


def _list_pairs() -> list[dict]:
    # The Files API can blip; a transient listing failure shouldn't 500 the
    # whole pairs endpoint — degrade to whatever we can list.
    try:
        originals = volume.list_docx(config.RAW_DIR)
    except Exception:
        log.exception("_list_pairs: could not list raw_documents")
        originals = []
    try:
        translated = volume.list_docx(config.TRANSLATED_DIR)
    except Exception:
        log.exception("_list_pairs: could not list translated_inplace")
        translated = []
    return volume.auto_pair(originals, translated)


def _resolve(pair_id: str) -> dict:
    """Return {pair_id, original_path, translated_path, target_lang}. Prefer the
    Lakebase row (canonical); fall back to a Volume scan + self-healing upsert."""
    db = None
    try:
        db = store.get_pair(pair_id)
    except Exception:
        log.warning("_resolve: Lakebase get_pair(%s) failed; falling back to Volume scan", pair_id, exc_info=True)
        db = None
    if db and db.get("original_path") and db.get("translated_path"):
        return {
            "pair_id": pair_id,
            "original_path": db["original_path"],
            "translated_path": db["translated_path"],
            "target_lang": db.get("target_lang"),
        }
    vol = next((x for x in _list_pairs() if x["pair_id"] == pair_id), None)
    if vol is None:
        raise HTTPException(404, f"pair not found: {pair_id}")
    store.upsert_pair({
        "pair_id": vol["pair_id"],
        "original_path": vol["original_path"],
        "translated_path": vol["translated_path"],
        "target_lang": vol.get("target_lang"),
        "total_paragraphs": None,
    })
    return {
        "pair_id": pair_id,
        "original_path": vol["original_path"],
        "translated_path": vol["translated_path"],
        "target_lang": vol.get("target_lang"),
    }


def _ensure_confidence(pair_id: str, orig_paras, tran_paras, source_lang, target_lang) -> dict[int, dict]:
    stored = store.get_confidence(pair_id)
    missing = []
    n = min(len(orig_paras), len(tran_paras))
    src = (source_lang or "en").lower()
    tgt = (target_lang or "tr").lower()
    for i in range(n):
        if i in stored:
            continue
        b = conf_mod.compute(
            orig_paras[i].get("text") or "",
            tran_paras[i].get("text") or "",
            source_lang=src, target_lang=tgt,
        )
        row = b.to_dict()
        row.update({"paragraph_idx": i, "source_lang": src, "target_lang": tgt})
        missing.append(row)
        stored[i] = row
    if missing:
        try:
            store.bulk_upsert_confidence(pair_id, missing)
        except Exception:
            pass
    return stored


def _confidence_flags(c: dict) -> list[str]:
    flags = []
    if c.get("untranslated_pct", 0) and c["untranslated_pct"] > 0.15:
        flags.append("untranslated-residue")
    lr = c.get("length_ratio")
    if lr is not None and (lr < 0.4 or lr > 3.0):
        flags.append("length-anomaly")
    if c.get("repeated_ngrams", 0) and c["repeated_ngrams"] >= 3:
        flags.append("repetition-loop")
    return flags


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    # Runtime settings (Volume-backed) override the deploy-time env defaults for
    # branding / target language / model, and carry the first-run gate flag.
    s = settings_mod.load()
    return {
        "reviewer": auth.reviewer(),
        "target_language": s["target_language"],
        "model_endpoint": s["model_endpoint"],
        "is_configured": s["is_configured"],
        "delta_sync_enabled": delta_sync.enabled(),
        "title": s["app_title"],
        "logo_url": s["logo_url"],
        "logo_alt": s["logo_alt"],
        "logo_width": config.APP_LOGO_WIDTH,
        "logo_height": config.APP_LOGO_HEIGHT,
    }


# ---------------------------------------------------------------------------
# Settings (first-run setup + admin) — Volume-backed, read by the pipeline too
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    """Full settings document for the Settings page (force-reads the Volume so
    an admin sees the persisted truth, not a possibly-stale cache)."""
    return settings_mod.load(force=True)


@app.put("/api/settings")
def put_settings(patch: dict = Body(...)):
    """Persist a partial settings update and mark the app configured. Accepts
    any of: model_endpoint, target_language, app_title, logo_url, logo_alt."""
    if not isinstance(patch, dict):
        raise HTTPException(400, "body must be a JSON object")
    return settings_mod.save(patch, actor=auth.reviewer())


# ---------------------------------------------------------------------------
# Pairs
# ---------------------------------------------------------------------------

@app.get("/api/pairs")
def list_pairs():
    pairs = _list_pairs()
    prog = {p["pair_id"]: p for p in store.list_pairs_with_progress()}
    out = []
    for p in pairs:
        d = prog.get(p["pair_id"], {})
        total = int(d.get("total_paragraphs") or 0)
        cert = int(d.get("certified") or 0)
        flg = int(d.get("flagged") or 0)
        out.append({
            "pair_id": p["pair_id"],
            "original_path": p["original_path"],
            "translated_path": p["translated_path"],
            "source_lang": d.get("source_lang"),
            "target_lang": p.get("target_lang") or d.get("target_lang"),
            "total_paragraphs": total,
            "lifecycle_state": d.get("lifecycle_state") or "UNDER_REVIEW",
            "locked": (d.get("lifecycle_state") in ("PUBLISHED", "PROMOTING", "ARCHIVED")),
            "certified": cert,
            "flagged": flg,
            "pending": max(0, total - cert - flg),
        })
    return out


@app.get("/api/pairs/{pair_id}")
def get_pair_detail(pair_id: str):
    match = _resolve(pair_id)
    orig_html, orig_paras = _render(match["original_path"])
    tran_html, tran_paras = _render(match["translated_path"])
    source_lang = docx_render.detect_lang(orig_paras)
    total = max(len(orig_paras), len(tran_paras))

    store.upsert_pair({
        "pair_id": pair_id,
        "original_path": match["original_path"],
        "translated_path": match["translated_path"],
        "target_lang": match["target_lang"],
        "total_paragraphs": total,
    })

    fb = {r["paragraph_idx"]: r for r in store.get_feedback(pair_id)}
    edits = store.get_edits(pair_id)
    conf = _ensure_confidence(pair_id, orig_paras, tran_paras, source_lang, match["target_lang"])
    locked = store.is_locked(pair_id)

    paragraphs = []
    for i in range(total):
        row = fb.get(i, {})
        c = conf.get(i, {})
        paragraphs.append({
            "idx": i,
            "page": (orig_paras[i]["page"] if i < len(orig_paras) else
                     (tran_paras[i]["page"] if i < len(tran_paras) else 1)),
            "source": orig_paras[i]["text"] if i < len(orig_paras) else "",
            "translated": tran_paras[i]["text"] if i < len(tran_paras) else "",
            "status": row.get("status") or "pending",
            "comment": row.get("comment"),
            "edited_text": edits.get(i),
            "reviewer": row.get("reviewer"),
            "confidence": c.get("confidence"),
            "confidence_flags": _confidence_flags(c),
        })

    return {
        "pair_id": pair_id,
        "original_path": match["original_path"],
        "translated_path": match["translated_path"],
        "source_lang": source_lang,
        "target_lang": match["target_lang"],
        "lifecycle_state": (store.get_pair(pair_id) or {}).get("lifecycle_state", "UNDER_REVIEW"),
        "locked": locked,
        "paragraphs": paragraphs,
    }


@app.get("/api/pairs/{pair_id}/preview/{side}", response_class=HTMLResponse)
def preview(pair_id: str, side: str):
    if side not in ("original", "translated"):
        raise HTTPException(400, "side must be 'original' or 'translated'")
    match = _resolve(pair_id)
    path = match["original_path"] if side == "original" else match["translated_path"]
    html, _ = _render(path)
    if side == "translated":
        edits = store.get_edits(pair_id)
        if edits:
            html = docx_render.apply_edits_overlay(html, edits)
    return HTMLResponse(html)


def _para_response(pair_id: str, idx: int) -> dict:
    """Re-read one paragraph's review row for the client after a mutation."""
    fb = {r["paragraph_idx"]: r for r in store.get_feedback(pair_id)}
    edits = store.get_edits(pair_id)
    conf = store.get_confidence(pair_id)
    row = fb.get(idx, {})
    c = conf.get(idx, {})
    match = _resolve(pair_id)
    _, orig_paras = _render(match["original_path"])
    _, tran_paras = _render(match["translated_path"])
    return {
        "idx": idx,
        "page": orig_paras[idx]["page"] if idx < len(orig_paras) else 1,
        "source": orig_paras[idx]["text"] if idx < len(orig_paras) else "",
        "translated": tran_paras[idx]["text"] if idx < len(tran_paras) else "",
        "status": row.get("status") or "pending",
        "comment": row.get("comment"),
        "edited_text": edits.get(idx),
        "reviewer": row.get("reviewer"),
        "confidence": c.get("confidence"),
        "confidence_flags": _confidence_flags(c),
    }


@app.post("/api/pairs/{pair_id}/paragraphs/{idx}/status")
def set_status(pair_id: str, idx: int = Path(..., ge=0), status: str = Body(..., embed=True)):
    if status not in ("pending", "certified", "flagged"):
        raise HTTPException(400, "invalid status")
    try:
        store.upsert_feedback(pair_id, idx, status, None, auth.reviewer())
    except store.PairLockedError as e:
        raise HTTPException(409, str(e))
    return _para_response(pair_id, idx)


@app.post("/api/pairs/{pair_id}/paragraphs/{idx}/comment")
def set_comment(pair_id: str, idx: int = Path(..., ge=0), comment: str = Body(..., embed=True)):
    try:
        store.upsert_feedback(pair_id, idx, None, comment, auth.reviewer())
    except store.PairLockedError as e:
        raise HTTPException(409, str(e))
    return _para_response(pair_id, idx)


@app.post("/api/pairs/{pair_id}/paragraphs/{idx}/edit")
def set_edit(pair_id: str, idx: int = Path(..., ge=0), edited_text: str | None = Body(None, embed=True)):
    try:
        store.upsert_edit(pair_id, idx, edited_text, auth.reviewer())
    except store.PairLockedError as e:
        raise HTTPException(409, str(e))
    return _para_response(pair_id, idx)


@app.post("/api/pairs/{pair_id}/certify-all")
def certify_all(pair_id: str):
    match = _resolve(pair_id)
    _, orig_paras = _render(match["original_path"])
    _, tran_paras = _render(match["translated_path"])
    total = max(len(orig_paras), len(tran_paras))
    try:
        n = store.bulk_upsert_feedback(
            pair_id, list(range(total)), "certified", auth.reviewer(),
            skip_commented=True,
        )
    except store.PairLockedError as e:
        raise HTTPException(409, str(e))
    return {"certified": n}


@app.post("/api/pairs/{pair_id}/certify-page")
def certify_page(pair_id: str, page: int = Body(..., embed=True)):
    """Certify every paragraph on a single page (the page containing the
    active paragraph). Uses the source-side page numbers from docx_render."""
    match = _resolve(pair_id)
    _, orig_paras = _render(match["original_path"])
    idxs = [p["idx"] for p in orig_paras if p.get("page") == page]
    if not idxs:
        return {"certified": 0, "page": page}
    try:
        n = store.bulk_upsert_feedback(
            pair_id, idxs, "certified", auth.reviewer(), skip_commented=True,
        )
    except store.PairLockedError as e:
        raise HTTPException(409, str(e))
    return {"certified": n, "page": page}


@app.post("/api/upload")
def upload_document(
    file: UploadFile = File(...),
    target_language: str = Form("English"),
    on_conflict: str = Form("rename"),  # "rename" (default) | "replace"
    prompt_id: int = Form(...),
):
    """Accept a source .docx, write it to raw_documents/. The file-arrival
    trigger on that folder kicks off the translation pipeline; the resulting
    pair shows up in /api/pairs once translation completes.

    Three per-file sidecars are written alongside the .docx so the pipeline
    picks up the reviewer's choices without changing the bundle default:
      * `.lang`   — the target language (source language is auto-detected).
      * `.user`   — the uploader's email (X-Forwarded-Email), surfaced in the
        Processing view so a reviewer can filter to their own uploads.
      * `.prompt` — a JSON snapshot {prompt_id, name, body} of the chosen prompt.
        The full body is FROZEN here so editing/deleting the prompt later never
        changes what this document was translated with.

    Same-name uploads are handled explicitly (on_conflict): 'rename' (default)
    auto-suffixes to keep both documents; 'replace' overwrites and clears the
    prior review state so certifications don't carry over to new content."""
    name = (file.filename or "").strip()
    if not name.lower().endswith(".docx"):
        raise HTTPException(400, "only .docx files are supported")
    if name.startswith("~$") or "/" in name or "\\" in name:
        raise HTTPException(400, "invalid filename")
    # Sync endpoint runs in FastAPI's threadpool (all the volume/DB calls below
    # block), so read the upload via the underlying sync file object.
    data = file.file.read()
    if not data:
        raise HTTPException(400, "empty file")

    # Prompt selection is required (enforced in the UI too). Resolve + snapshot.
    prompt = prompts_mod.get_prompt(prompt_id)
    if prompt is None:
        raise HTTPException(400, f"unknown prompt_id: {prompt_id}")

    # ---- Same-name collision handling -------------------------------------
    # pair_id is derived from the filename stem (volume.auto_pair), so an
    # upload that reuses a name would silently overwrite the source AND inherit
    # the existing pair's review state — certifications and edits made against
    # different content. That's a correctness hazard, not just confusing, so we
    # refuse by default and require an explicit choice:
    #   * rename (default)  — auto-suffix to keep both documents distinct
    #   * replace           — caller passes on_conflict=replace, knowing the
    #                         existing review state applies to new content
    stem = name[: -len(".docx")]
    existing = {f["name"] for f in volume.list_docx(config.RAW_DIR)}
    renamed_from = None
    if name in existing:
        if on_conflict == "replace":
            # Caller explicitly asked to overwrite. Drop the stale review state
            # so certifications don't carry over to different content.
            try:
                store.delete_pair_state(stem)
            except Exception:
                pass
        else:
            n = 2
            while f"{stem}_{n}.docx" in existing:
                n += 1
            renamed_from, name = name, f"{stem}_{n}.docx"

    dest = f"{config.RAW_DIR}/{name}"
    # Write the sidecars FIRST so they're already present when the .docx lands
    # and the file-arrival trigger fires. The watcher only scans .docx files.
    try:
        volume.upload_docx(f"{config.RAW_DIR}/{name}.lang", target_language.encode("utf-8"))
    except Exception:
        pass
    # Uploader identity (from X-Forwarded-Email). The watcher reads this sidecar
    # into bronze_documents.submitted_by so the Processing view can filter to the
    # current user. Best-effort — a missing sidecar just yields NULL submitted_by.
    try:
        volume.upload_docx(f"{config.RAW_DIR}/{name}.user", auth.reviewer().encode("utf-8"))
    except Exception:
        pass
    # Frozen prompt snapshot — the pipeline reads this, not the live table.
    snapshot = json.dumps({
        "prompt_id": prompt["prompt_id"],
        "name": prompt["name"],
        "body": prompt["body"],
    })
    volume.upload_docx(f"{config.RAW_DIR}/{name}.prompt", snapshot.encode("utf-8"))
    # The document last — its arrival is what triggers the pipeline.
    volume.upload_docx(dest, data)

    if renamed_from:
        message = (
            f"A document named {renamed_from} already exists, so this was uploaded "
            f"as {name} to keep them separate. Translation runs on file arrival; "
            f"it will appear in the list once it completes (usually 1–3 min)."
        )
    elif on_conflict == "replace":
        message = (
            f"Replaced {name} and cleared its previous review state. Translation "
            f"runs on file arrival; it will reappear once it completes."
        )
    else:
        message = ("Uploaded. Translation runs on file arrival; the pair will "
                   "appear in the list once it completes (usually 1–3 min).")

    return {
        "ok": True,
        "name": name,
        "path": dest,
        "target_language": target_language,
        "renamed_from": renamed_from,
        "prompt_id": prompt["prompt_id"],
        "prompt_name": prompt["name"],
        "message": message,
    }


@app.get("/api/documents")
def list_documents():
    """Pipeline status for every raw document: what's landed, translating,
    translated, or failed. Reads the bronze_documents Delta table (populated by
    the watcher) and cross-references raw_documents/ so a just-uploaded file
    that hasn't reached bronze yet shows as QUEUED rather than vanishing."""
    rows: list[dict] = []
    bronze_names: set[str] = set()

    # Bronze status via the SQL warehouse (defensive: source_language column
    # only exists after the first pipeline run on the updated watcher).
    if delta_sync.enabled():
        fqn = f"{delta_sync.DELTA_CATALOG}.{delta_sync.DELTA_SCHEMA}.bronze_documents"
        for cols in (
            "file_name, translation_status, target_language, source_language, "
            "translation_started_at, translation_ended_at, translation_error",
            # Fallback for pre-migration tables without source_language.
            "file_name, translation_status, target_language, "
            "translation_started_at, translation_ended_at, translation_error",
        ):
            try:
                out = delta_sync._execute(
                    f"SELECT {cols} FROM {fqn} ORDER BY first_seen_at DESC LIMIT 200"
                )
                data = (out.get("result") or {}).get("data_array") or []
                has_src = "source_language" in cols
                for r in data:
                    name = r[0]
                    bronze_names.add(name)
                    if has_src:
                        rows.append({
                            "file_name": name, "status": r[1], "target_language": r[2],
                            "source_language": r[3], "started_at": r[4],
                            "ended_at": r[5], "error": r[6],
                        })
                    else:
                        rows.append({
                            "file_name": name, "status": r[1], "target_language": r[2],
                            "source_language": None, "started_at": r[3],
                            "ended_at": r[4], "error": r[5],
                        })
                break  # first query variant that succeeds wins
            except Exception:
                continue

    # Raw files not yet in bronze → QUEUED (uploaded, waiting on the trigger).
    try:
        for f in volume.list_docx(config.RAW_DIR):
            if f["name"] not in bronze_names:
                rows.append({
                    "file_name": f["name"], "status": "QUEUED", "target_language": None,
                    "source_language": None, "started_at": None, "ended_at": None,
                    "error": None,
                })
    except Exception:
        pass

    return {"documents": rows, "warehouse_configured": delta_sync.enabled()}


# --- Processing status (per-user, Jobs-API-driven) --------------------------

PIPELINE_JOB_NAME = "doc-translation · auto-translate pipeline"
_pipeline_job_id: int | None = None
_pipeline_job_looked_up = False


def _pipeline_job_id_cached() -> int | None:
    """The shared translation pipeline's job id, resolved by name once and
    memoized (it's stable for the app's lifetime). None if not found."""
    global _pipeline_job_id, _pipeline_job_looked_up
    if _pipeline_job_looked_up:
        return _pipeline_job_id
    _pipeline_job_looked_up = True
    try:
        for j in config.w().jobs.list():
            if j.settings and j.settings.name == PIPELINE_JOB_NAME:
                _pipeline_job_id = j.job_id
                break
    except Exception:
        _pipeline_job_id = None
    return _pipeline_job_id


def _pipeline_status() -> dict:
    """Whether the shared pipeline is actively running right now, and for how
    long — driven by the Jobs API. Best-effort: any failure returns an inactive
    pipeline so the document list still renders. Note the run is a shared batch
    across all users, so this is a global signal, not per-user."""
    job_id = _pipeline_job_id_cached()
    out = {"job_id": job_id, "active": False, "started_at_ms": None, "elapsed_seconds": None}
    if job_id is None:
        return out
    try:
        runs = list(config.w().jobs.list_runs(job_id=job_id, active_only=True))
    except Exception:
        return out
    if not runs:
        return out
    out["active"] = True
    starts = [r.start_time for r in runs if getattr(r, "start_time", None)]
    if starts:
        earliest = min(starts)  # epoch millis
        out["started_at_ms"] = earliest
        out["elapsed_seconds"] = max(0, int(time.time() * 1000 - earliest) // 1000)
    return out


@app.get("/api/processing-status")
def processing_status():
    """Documents the CURRENT user has submitted that are queued/translating,
    plus their recently finished/failed ones (last 24h), each with an elapsed
    time. The pipeline block reports whether the shared translation job is
    actively running (Jobs API). See /api/documents for the workspace-wide view.

    Per-user scoping is by bronze_documents.submitted_by (captured at upload via
    the `.user` sidecar). Freshly-uploaded files not yet in bronze are attributed
    by reading that same sidecar, so a user sees their upload as QUEUED at once."""
    user = auth.reviewer()
    rows: list[dict] = []
    bronze_names: set[str] = set()

    if delta_sync.enabled():
        fqn = f"{delta_sync.DELTA_CATALOG}.{delta_sync.DELTA_SCHEMA}.bronze_documents"
        # elapsed_seconds computed in SQL to avoid fragile timestamp-string
        # parsing: for in-flight rows measure to now, else start→end.
        q = f"""
            SELECT file_name, translation_status, target_language, source_language,
                   translation_started_at, translation_ended_at, translation_error,
                   CAST(
                     unix_timestamp(
                       CASE WHEN translation_status = 'TRANSLATING'
                            THEN current_timestamp()
                            ELSE coalesce(translation_ended_at, current_timestamp()) END
                     ) - unix_timestamp(translation_started_at)
                   AS BIGINT) AS elapsed_seconds
            FROM {fqn}
            WHERE submitted_by = {delta_sync._esc(user)}
              AND (translation_status IN ('QUEUED', 'TRANSLATING')
                   OR translation_ended_at >= current_timestamp() - INTERVAL 24 HOURS)
            ORDER BY first_seen_at DESC
            LIMIT 200
        """
        # Defensive: submitted_by only exists after the migration/first watcher
        # run. On any failure we leave bronze rows empty (never fall back to an
        # unscoped query that would leak other users' documents) — the sidecar
        # path below still surfaces this user's freshly-uploaded files.
        try:
            out = delta_sync._execute(q)
            data = (out.get("result") or {}).get("data_array") or []
            for r in data:
                name = r[0]
                bronze_names.add(name)
                elapsed = None
                if r[7] is not None:
                    try:
                        elapsed = max(0, int(r[7]))
                    except (TypeError, ValueError):
                        elapsed = None
                rows.append({
                    "file_name": name, "status": r[1], "target_language": r[2],
                    "source_language": r[3], "started_at": r[4], "ended_at": r[5],
                    "error": r[6], "elapsed_seconds": elapsed,
                })
        except Exception:
            pass

    # Raw files not yet in bronze → QUEUED, but only the ones THIS user uploaded
    # (attributed via the `.user` sidecar written at upload time).
    try:
        for f in volume.list_docx(config.RAW_DIR):
            if f["name"] in bronze_names:
                continue
            owner = volume.read_text(f"{config.RAW_DIR}/{f['name']}.user")
            if owner != user:
                continue
            rows.append({
                "file_name": f["name"], "status": "QUEUED", "target_language": None,
                "source_language": None, "started_at": None, "ended_at": None,
                "error": None, "elapsed_seconds": None,
            })
    except Exception:
        pass

    return {
        "user": user,
        "pipeline": _pipeline_status(),
        "documents": rows,
        "warehouse_configured": delta_sync.enabled(),
    }


@app.post("/api/pairs/{pair_id}/publish")
def publish(pair_id: str):
    match = _resolve(pair_id)
    pending = store.get_pending_edits(pair_id)
    if not pending:
        raise HTTPException(400, "no pending edits to publish")
    version = store.next_publish_version(pair_id)
    target_path = volume.reviewed_path(pair_id, match["target_lang"], version)
    all_edits = store.get_edits(pair_id)
    tran_bytes = volume.read_docx(match["translated_path"])
    new_bytes, applied = docx_render.apply_edits_to_docx(tran_bytes, all_edits)
    volume.upload_docx(target_path, new_bytes)
    store.record_publish(pair_id, target_path, applied, auth.reviewer())
    return {"output_path": target_path, "edits_applied": applied, "version": version}


@app.post("/api/pairs/{pair_id}/promote")
def promote(pair_id: str):
    match = _resolve(pair_id)
    actor = auth.reviewer()
    ready, reason = store.is_ready_for_gold(pair_id)
    if not ready:
        raise HTTPException(409, f"not ready for gold: {reason}")

    prog = store.progress_for(pair_id)
    publish_history = store.list_publish_log(pair_id)
    reviewers = store.get_distinct_reviewers(pair_id)

    store.begin_gold_promotion(pair_id, actor)
    try:
        orig_bytes = volume.read_docx(match["original_path"])
        tran_source = publish_history[0]["output_path"] if publish_history else match["translated_path"]
        tran_bytes = volume.read_docx(tran_source)
        copy_info = volume.copy_to_golden(
            pair_id=pair_id,
            target_lang=match["target_lang"] or "tr",
            original_bytes=orig_bytes,
            translated_bytes=tran_bytes,
        )
        result = store.complete_gold_promotion(
            pair_id=pair_id, actor=actor,
            golden_original_path=copy_info["golden_original_path"],
            golden_translated_path=copy_info["golden_translated_path"],
            golden_original_hash=copy_info["golden_original_hash"],
            golden_translated_hash=copy_info["golden_translated_hash"],
            total_paragraphs=prog["total"],
            certified_paragraphs=prog["certified"],
            edits_applied=sum(p["edits_applied"] for p in publish_history),
            distinct_reviewers=reviewers,
        )
    except Exception as e:
        try:
            store.abort_gold_promotion(pair_id, actor, str(e))
        except Exception:
            pass
        raise HTTPException(500, f"promotion failed: {e}")

    delta_synced, message = False, "Promoted (Delta sync skipped — no warehouse)."
    try:
        sync = delta_sync.sync_pair_to_delta(pair_id, publication_id=result["publication_id"])
        if not sync.get("skipped"):
            delta_synced = True
            c = sync["counts"]
            message = (f"Promoted · publication #{result['publication_id']} · "
                       f"Delta: {c['audit_events']} events, {c['silver_review_snapshots']} snapshots")
    except Exception as sync_err:
        message = f"Promoted (Delta sync deferred: {sync_err})"
    return {"ok": True, "delta_synced": delta_synced, "message": message}


@app.get("/api/pairs/{pair_id}/audit")
def audit(pair_id: str):
    events = store.list_audit_events(pair_id, limit=500)
    return [{
        "event_id": e["event_id"],
        "event_type": e["event_type"],
        "actor": e["actor"],
        "actor_type": e.get("actor_type") or "human",
        "paragraph_idx": e.get("paragraph_idx"),
        "event_at": str(e["event_at"]),
    } for e in events]


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

@app.get("/api/glossary")
def glossary_list(source: str | None = None, approved: bool | None = None):
    entries = glossary_mod.list_glossary(
        source=source,
        approved_only=bool(approved) if approved else False,
        limit=2000,
    )
    return [{
        "entry_id": e["entry_id"],
        "source_lang": e["source_lang"],
        "target_lang": e["target_lang"],
        "model_phrase": e["model_phrase"],
        "correction": e["correction"],
        "occurrences": e["occurrences"],
        "distinct_reviewers": e["distinct_reviewers"],
        "approved": e["approved"],
        "source": e.get("source") or "tenant",
        "last_seen_at": str(e["last_seen_at"]),
    } for e in entries]


@app.post("/api/glossary/{entry_id}/approve")
def glossary_approve(entry_id: int, approved: bool = Body(..., embed=True)):
    glossary_mod.toggle_approval(entry_id, approved)
    entries = {e["entry_id"]: e for e in glossary_mod.list_glossary(approved_only=False, limit=5000)}
    e = entries.get(entry_id)
    if not e:
        raise HTTPException(404, "entry not found")
    return {
        "entry_id": e["entry_id"], "source_lang": e["source_lang"], "target_lang": e["target_lang"],
        "model_phrase": e["model_phrase"], "correction": e["correction"],
        "occurrences": e["occurrences"], "distinct_reviewers": e["distinct_reviewers"],
        "approved": e["approved"], "source": e.get("source") or "tenant",
        "last_seen_at": str(e["last_seen_at"]),
    }


@app.post("/api/glossary/import")
def glossary_import(file: UploadFile = File(...)):
    # Sync endpoint (ingest does blocking DB work) → runs in the threadpool;
    # read via the underlying sync file object.
    data = file.file.read()
    n = glossary_mod.ingest_glossary_csv(data, source="customer", approved=True)
    return {"imported": n}


@app.post("/api/glossary/mine")
def glossary_mine():
    n = glossary_mod.mine_glossary()
    return {"mined": n}


@app.post("/api/glossary/sync-delta")
def glossary_sync():
    res = delta_sync.sync_glossary_to_delta()
    return {"rows": res.get("rows", 0), "skipped": bool(res.get("skipped"))}


# ---------------------------------------------------------------------------
# Translation prompts ("Instructions") — full CRUD over named system prompts.
# One is chosen per document at upload time (snapshotted into a .prompt sidecar).
# ---------------------------------------------------------------------------

def _prompt_out(p: dict) -> dict:
    return {
        "prompt_id": p["prompt_id"],
        "name": p["name"],
        "body": p["body"],
        "description": p.get("description"),
        "created_by": p.get("created_by"),
        "created_at": str(p["created_at"]) if p.get("created_at") else None,
        "updated_by": p.get("updated_by"),
        "updated_at": str(p["updated_at"]) if p.get("updated_at") else None,
    }


@app.get("/api/prompts")
def prompts_list():
    return [_prompt_out(p) for p in prompts_mod.list_prompts()]


@app.get("/api/prompts/template")
def prompts_template():
    """The built-in default prompt text — the editor pre-fills new prompts with
    it (seeded-editable) and offers a 'reset to default template' action."""
    return {"body": prompts_mod.DEFAULT_PROMPT_BODY}


@app.get("/api/prompts/{prompt_id}")
def prompts_get(prompt_id: int):
    p = prompts_mod.get_prompt(prompt_id)
    if p is None:
        raise HTTPException(404, "prompt not found")
    return _prompt_out(p)


@app.post("/api/prompts")
def prompts_create(
    name: str = Body(..., embed=True),
    body: str = Body(..., embed=True),
    description: str | None = Body(None, embed=True),
):
    try:
        p = prompts_mod.create_prompt(
            name=name, body=body, description=description, actor=auth.reviewer())
    except prompts_mod.DuplicateNameError:
        raise HTTPException(409, f"a prompt named '{name.strip()}' already exists")
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    return _prompt_out(p)


@app.put("/api/prompts/{prompt_id}")
def prompts_update(
    prompt_id: int,
    name: str = Body(..., embed=True),
    body: str = Body(..., embed=True),
    description: str | None = Body(None, embed=True),
):
    try:
        p = prompts_mod.update_prompt(
            prompt_id, name=name, body=body, description=description, actor=auth.reviewer())
    except prompts_mod.DuplicateNameError:
        raise HTTPException(409, f"a prompt named '{name.strip()}' already exists")
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    if p is None:
        raise HTTPException(404, "prompt not found")
    return _prompt_out(p)


@app.delete("/api/prompts/{prompt_id}")
def prompts_delete(prompt_id: int):
    if not prompts_mod.delete_prompt(prompt_id, actor=auth.reviewer()):
        raise HTTPException(404, "prompt not found")
    return {"ok": True}


@app.post("/api/prompts/{prompt_id}/clone")
def prompts_clone(prompt_id: int, name: str | None = Body(None, embed=True)):
    try:
        p = prompts_mod.clone_prompt(prompt_id, new_name=name, actor=auth.reviewer())
    except prompts_mod.DuplicateNameError:
        raise HTTPException(409, f"a prompt named '{(name or '').strip()}' already exists")
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    if p is None:
        raise HTTPException(404, "prompt not found")
    return _prompt_out(p)


# ---------------------------------------------------------------------------
# Static SPA (mounted last so /api/* wins)
# ---------------------------------------------------------------------------

if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
