"""Doc Translation Review — Streamlit app.

Side-by-side DOCX viewer with paragraph-level certify / flag / comment.
Pairs are picked from a Volume folder, feedback is persisted to Lakebase Postgres.
"""
from __future__ import annotations

import streamlit as st

# Initialize pool once and configure page before importing UI components.
st.set_page_config(
    page_title="Doc Translation Review",
    page_icon="✅",
    layout="wide",
    initial_sidebar_state="expanded",
)

from server import config, volume, store, docx_render, auth, delta_sync
from server import confidence as conf_mod
from server import glossary as glossary_mod
from server.db import pool
from server.styles import CSS
from components.dual_pane import dual_pane

# ----------------------------------------------------------------------------
# One-time pool open
# ----------------------------------------------------------------------------
if "pool_opened" not in st.session_state:
    pool.open(wait=True, timeout=30.0)
    st.session_state["pool_opened"] = True

# Show a one-shot Delta-sync status banner after a promote (cleared after view).
if "last_delta_msg" in st.session_state:
    st.info(st.session_state.pop("last_delta_msg"))

# Schema migrations are run separately by an admin (the table owner) via
# `python -c "from server import store; store.ensure_schema()"`. The app
# service principal doesn't own the existing tables, so it can't ALTER them
# at runtime — which is the conventional pattern anyway: DDL is a deploy-time
# concern, not a request-path concern.


# ----------------------------------------------------------------------------
# Defensive helpers
# ----------------------------------------------------------------------------
def _normalize_pair_id(s: str | None) -> str | None:
    """Recover a raw pair_id from a possibly-formatted display string.

    Reality has been that Streamlit's selectbox occasionally leaks its
    `format_func` output into the value stored under its key (or stale
    session_state survives across UI revisions). The formatted display
    follows the shape:

        [glyph ]<pair_id>[ · <progress|status>][ ⚑N]

    Strip the glyph prefix and the " · …" suffix to recover the raw id.
    Idempotent on already-clean strings."""
    if not s:
        return s
    for g in ("🔒 ", "⏳ ", "📦 "):
        if s.startswith(g):
            s = s[len(g):]
            break
    if " · " in s:
        s = s.split(" · ", 1)[0]
    return s.strip()


# ----------------------------------------------------------------------------
# Cached helpers
# ----------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def list_volume_cached():
    """Returns (pairs, unpaired_originals) — single Volume scan per cache TTL.
    On transient Files-API failure we cache an empty result for the TTL window;
    callers (the main flow) bust the cache and retry once if the selected pair
    is missing — see the resolve block below."""
    try:
        originals = volume.list_docx(config.RAW_DIR)
    except Exception:
        originals = []
    try:
        translated = volume.list_docx(config.TRANSLATED_DIR)
    except Exception:
        translated = []
    return volume.auto_pair(originals, translated), volume.unpaired_originals(originals, translated)


@st.cache_data(ttl=600, show_spinner="Rendering documents…")
def render_pair(orig_path: str, tran_path: str):
    orig_html, orig_paras = docx_render.render(volume.read_docx(orig_path))
    tran_html, tran_paras = docx_render.render(volume.read_docx(tran_path))
    source_lang = docx_render.detect_lang(orig_paras)
    return orig_html, orig_paras, tran_html, tran_paras, source_lang


# ----------------------------------------------------------------------------
# Custom CSS — kill Streamlit chrome we don't want, polish the shell
# ----------------------------------------------------------------------------
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Sidebar — pair picker
# ----------------------------------------------------------------------------
with st.sidebar:
    st.markdown('<div class="app-title">✓ Translation Review</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="app-sub">Reviewer: <b>{auth.reviewer()}</b></div>', unsafe_allow_html=True)

    st.markdown("**Pairs**")
    if st.button("↻ Refresh", use_container_width=True):
        list_volume_cached.clear()
        st.rerun()

    # Search/filter — substring match against pair_id. Empty = show everything.
    search_q = st.text_input(
        "Search pairs",
        placeholder="filter by name…",
        key="sidebar-search",
        label_visibility="collapsed",
    ).strip().lower()

    try:
        pairs, unpaired = list_volume_cached()
    except Exception as e:
        st.error(f"Could not list pairs: {e}")
        pairs, unpaired = [], []

    db_progress = {p["pair_id"]: p for p in store.list_pairs_with_progress()}

    # Apply the search filter
    if search_q:
        filtered_pairs = [p for p in pairs if search_q in p["pair_id"].lower()]
    else:
        filtered_pairs = pairs

    def _format_pair_option(pair_id: str) -> str:
        prog = db_progress.get(pair_id, {})
        total  = prog.get("total_paragraphs") or 0
        cert   = int(prog.get("certified") or 0)
        flagd  = int(prog.get("flagged")   or 0)
        state  = prog.get("lifecycle_state") or "UNDER_REVIEW"
        glyph  = {"PUBLISHED": "🔒 ", "PROMOTING": "⏳ ", "ARCHIVED": "📦 "}.get(state, "")
        name   = pair_id if len(pair_id) <= 42 else pair_id[:39] + "…"
        if state == "PUBLISHED":
            tail = " · published"
        elif total:
            pct = int(100 * cert / total) if total else 0
            tail = f" · {cert}/{total} ({pct}%)" + (f" ⚑{flagd}" if flagd else "")
        else:
            tail = " · not started"
        return f"{glyph}{name}{tail}"

    def _on_pair_change():
        selected = st.session_state.get("sidebar-pair-select")
        if not selected:
            return
        # Defensive: never store the formatted display string. If Streamlit's
        # selectbox handed us back the format_func output (or matched against
        # one), recover the raw pair_id.
        selected = _normalize_pair_id(selected)
        st.session_state["pair_id"] = selected
        p = next((x for x in pairs if x["pair_id"] == selected), None)
        if p is not None:
            store.upsert_pair({
                "pair_id":          p["pair_id"],
                "original_path":    p["original_path"],
                "translated_path":  p["translated_path"],
                "target_lang":      p["target_lang"],
                "total_paragraphs": None,
            })

    if not pairs:
        st.info(
            "No pairs found. Drop matching DOCX files into:\n\n"
            f"`{config.RAW_DIR}`\n\nand\n\n`{config.TRANSLATED_DIR}`"
        )
    elif not filtered_pairs:
        st.caption(f"No pairs match “{search_q}”")
    else:
        options = [p["pair_id"] for p in filtered_pairs]
        current_pair_id = _normalize_pair_id(st.session_state.get("pair_id"))
        # If we recovered a different value (heal stale state in place), write
        # it back so the rest of the script sees the clean value too.
        if current_pair_id and current_pair_id != st.session_state.get("pair_id"):
            st.session_state["pair_id"] = current_pair_id
        initial_idx = options.index(current_pair_id) if current_pair_id in options else None

        st.selectbox(
            "Pair",
            options=options,
            format_func=_format_pair_option,
            index=initial_idx,
            placeholder="Select a file pair…",
            key="sidebar-pair-select",
            on_change=_on_pair_change,
            label_visibility="collapsed",
        )
        st.caption(f"{len(filtered_pairs)} of {len(pairs)} pair(s)")

    # Unpaired raw files — uploaded but no translation yet.
    if unpaired:
        with st.expander(f"📥 Awaiting translation ({len(unpaired)})", expanded=False):
            st.caption(
                "Files in `raw_documents/` that have no matching file in "
                "`translated_inplace/`. The translation pipeline may still be "
                "running, or it hasn't been kicked off."
            )
            for f in sorted(unpaired, key=lambda x: x.get("modified") or "", reverse=True):
                modified = f.get("modified") or ""
                modified_short = str(modified)[:16] if modified else ""
                size_kb = round((f.get("size") or 0) / 1024)
                st.markdown(
                    f'<div style="font-size:11px; line-height:1.35; '
                    f'font-family:var(--mono); padding:4px 0; '
                    f'border-bottom:1px solid var(--rule-soft);">'
                    f'<b>{f["name"]}</b><br/>'
                    f'<span style="color:var(--ink-mute);">{modified_short} · {size_kb} KB</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


# ----------------------------------------------------------------------------
# Main area
# ----------------------------------------------------------------------------
pair_id = _normalize_pair_id(st.session_state.get("pair_id"))
# Heal stale state in place so downstream reads stay clean.
if pair_id and pair_id != st.session_state.get("pair_id"):
    st.session_state["pair_id"] = pair_id

if not pair_id:
    st.markdown('<div class="app-title">Doc Translation Review</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="app-sub">Pick a file pair from the sidebar to start reviewing. '
        'Hover the original to highlight the matching translated paragraph; click any paragraph to focus it for certify/comment.</div>',
        unsafe_allow_html=True,
    )
    st.stop()

# Resolve the pair's paths. Lakebase is the source of truth (it owns
# `original_path` + `translated_path` on `review_pairs`) — the Volume scan
# is only a fallback for newly-discovered pairs.
def _match_from_db(row: dict) -> dict:
    return {
        "pair_id":         pair_id,
        "original_path":   row["original_path"],
        "translated_path": row["translated_path"],
        "target_lang":     row.get("target_lang"),
        "original_name":   row["original_path"].rsplit("/", 1)[-1],
        "translated_name": row["translated_path"].rsplit("/", 1)[-1],
    }

def _match_from_vol(p: dict) -> dict:
    return {
        "pair_id":         p["pair_id"],
        "original_path":   p["original_path"],
        "translated_path": p["translated_path"],
        "target_lang":     p.get("target_lang"),
        "original_name":   p.get("original_name") or p["original_path"].rsplit("/", 1)[-1],
        "translated_name": p.get("translated_name") or p["translated_path"].rsplit("/", 1)[-1],
    }

match = None
diag = {}  # diagnostic trail so we can see exactly what failed if we bounce out

# Try Lakebase first (canonical), with retries — a single blip is recoverable.
db_pair = None
db_err = None
for attempt in (1, 2, 3):
    try:
        db_pair = store.get_pair(pair_id)
        db_err = None
        break
    except Exception as e:
        db_err = str(e)
diag["db_attempts"] = attempt
diag["db_err"] = db_err
diag["db_row_found"] = bool(db_pair)
diag["db_paths_set"] = bool(db_pair and db_pair.get("original_path") and db_pair.get("translated_path"))

if db_pair and db_pair.get("original_path") and db_pair.get("translated_path"):
    match = _match_from_db(db_pair)
else:
    # Lakebase didn't give us a usable row. Try the Volume scan + self-heal
    # upsert (covers the case where the sidebar callback's upsert silently failed).
    vol_match = next((x for x in pairs if x["pair_id"] == pair_id), None)
    diag["vol_in_cache"] = bool(vol_match)
    if vol_match is None:
        list_volume_cached.clear()
        try:
            pairs, unpaired = list_volume_cached()
        except Exception:
            pass
        vol_match = next((x for x in pairs if x["pair_id"] == pair_id), None)
        diag["vol_in_refetch"] = bool(vol_match)

    if vol_match is not None:
        try:
            store.upsert_pair({
                "pair_id":          vol_match["pair_id"],
                "original_path":    vol_match["original_path"],
                "translated_path":  vol_match["translated_path"],
                "target_lang":      vol_match.get("target_lang"),
                "total_paragraphs": None,
            })
        except Exception as e:
            st.error(f"Could not register pair in Lakebase: {e}")
            st.stop()
        match = _match_from_vol(vol_match)

if match is None:
    # Couldn't resolve. Don't bounce — show a soft retry instead of clearing
    # the user's selection. Transient Lakebase / Files-API blips shouldn't
    # cost them their working context.
    st.warning(
        f"Couldn't load `{pair_id}` right now. This is usually transient — "
        f"click Retry. If it keeps happening, pick another pair from the sidebar."
    )
    with st.expander("Diagnostic (helps us debug if this persists)", expanded=False):
        st.json(diag)
    if st.button("↻ Retry", type="primary", key=f"retry-resolve-{pair_id}"):
        list_volume_cached.clear()
        st.rerun()
    if st.button("Clear selection", key=f"clear-pair-{pair_id}"):
        st.session_state.pop("pair_id", None)
        st.session_state.pop("sidebar-pair-select", None)
        st.rerun()
    st.stop()

orig_html, orig_paras, tran_html, tran_paras, source_lang = render_pair(match["original_path"], match["translated_path"])
total_paragraphs = max(len(orig_paras), len(tran_paras))
target_lang_chip = (match["target_lang"] or "tr").upper()
source_lang_chip = source_lang.upper()

# Persist the now-known total
store.upsert_pair({
    "pair_id":          pair_id,
    "original_path":    match["original_path"],
    "translated_path":  match["translated_path"],
    "target_lang":      match["target_lang"],
    "total_paragraphs": total_paragraphs,
})

# Load feedback for this pair
feedback_rows = store.get_feedback(pair_id)
feedback_map: dict = {row["paragraph_idx"]: row for row in feedback_rows}

# Lazy heuristic confidence: compute for any paragraph that doesn't have a
# stored score yet, then upsert. Cheap (no LLM), runs once per (pair, paragraph)
# since we never re-translate — except when the user explicitly forces a refresh
# via the sidebar button.
def _ensure_confidence(force: bool = False) -> dict[int, dict]:
    stored = {} if force else store.get_confidence(pair_id)
    missing = []
    n_para = min(len(orig_paras), len(tran_paras))
    src_lang = (source_lang or "en").lower()
    tgt_lang = (match.get("target_lang") or "tr").lower()
    for i in range(n_para):
        if i in stored:
            continue
        src_text = orig_paras[i].get("text") or ""
        tgt_text = tran_paras[i].get("text") or ""
        b = conf_mod.compute(src_text, tgt_text, source_lang=src_lang, target_lang=tgt_lang)
        row = b.to_dict()
        row["paragraph_idx"] = i
        row["source_lang"]   = src_lang
        row["target_lang"]   = tgt_lang
        missing.append(row)
        stored[i] = row
    if missing:
        try:
            store.bulk_upsert_confidence(pair_id, missing)
        except Exception:
            pass  # best-effort; never block the UI on this
    return stored

confidence_map = _ensure_confidence()

# Live edit overlay — applied to translated HTML before the pane renders so
# the reviewer sees their in-flight changes alongside originals.
edits_map: dict[int, str] = store.get_edits(pair_id)
if edits_map:
    tran_html = docx_render.apply_edits_overlay(tran_html, edits_map)

# Progress numbers + lifecycle state
prog = store.progress_for(pair_id)
lifecycle_state = prog["lifecycle_state"]
is_locked = lifecycle_state in ("PUBLISHED", "ARCHIVED")
is_promoting = lifecycle_state == "PROMOTING"
publication = store.get_publication(pair_id) if is_locked else None
is_ready_for_gold, gold_block_reason = (
    store.is_ready_for_gold(pair_id) if not is_locked and not is_promoting
    else (False, f"document is in state {lifecycle_state}")
)

# Audit: emit OPENED once per (pair, session). The session_state guard
# prevents 1 event per Streamlit re-run.
_opened_key = f"opened-emitted-{pair_id}"
if not st.session_state.get(_opened_key):
    try:
        client_ip = st.context.headers.get("X-Forwarded-For") if hasattr(st, "context") else None
    except Exception:
        client_ip = None
    try:
        store.record_open(pair_id, auth.reviewer(), client_ip=client_ip)
    except Exception:
        pass  # never block the UI on audit telemetry
    st.session_state[_opened_key] = True


# ----------------------------------------------------------------------------
# Header bar — compact, single row: title + progress
# ----------------------------------------------------------------------------
total = max(prog["total"], 1)
cert_pct = prog["certified"] / total * 100
flag_pct = prog["flagged"]   / total * 100
pend_pct = max(0, 100 - cert_pct - flag_pct)
pending_n = max(0, prog["total"] - prog["certified"] - prog["flagged"])

# Pending edits (= edits not yet baked into a published reviewed DOCX) drive
# the header CTAs and the gold-promotion gate. `edits_map` (every current edit
# overlay, published or not) is what we feed into the pane render so the
# reviewer keeps seeing their edited text after publishing.
pending_edit_count = store.count_pending_edits(pair_id)
edit_count = pending_edit_count  # legacy alias used downstream in header text

# Aggregated confidence stats (used by the header chip + minimap coloring).
HIGH_CONF_THRESHOLD = 0.9
LOW_CONF_THRESHOLD  = 0.6
high_conf_count = sum(1 for c in confidence_map.values() if c["confidence"] >= HIGH_CONF_THRESHOLD)
low_conf_count  = sum(1 for c in confidence_map.values() if c["confidence"] <  LOW_CONF_THRESHOLD)
publish_history = store.list_publish_log(pair_id)
last_publish = publish_history[0] if publish_history else None

# Lifecycle chip — single source of truth for read/write capabilities downstream.
state_chip_colors = {
    "UNDER_REVIEW": ("#0f766e", "var(--accent-soft)"),
    "PROMOTING":    ("#b45309", "rgba(180,83,9,0.15)"),
    "PUBLISHED":    ("#15803d", "#dcfce7"),
    "ARCHIVED":     ("#475569", "#e2e8f0"),
}
fg_color, bg_color = state_chip_colors.get(lifecycle_state, ("#475569", "#e2e8f0"))
state_label = lifecycle_state.replace("_", " ")

hdr_l, hdr_m, hdr_r = st.columns([3, 2, 1.2])
with hdr_l:
    progress_pct = int(round(cert_pct))
    progress_chip = (
        f'<span class="progress-chip" '
        f'style="background:var(--accent-soft); color:var(--accent-strong);">'
        f'{prog["certified"]}/{prog["total"]} · {progress_pct}%</span>'
    )
    st.markdown(
        f'<div class="main-title-block">'
        f'<div class="app-title">{pair_id}'
        f'<span class="lang-tag">{source_lang_chip} → {target_lang_chip}</span>'
        f'<span class="lifecycle-chip" '
        f'style="background:{bg_color}; color:{fg_color};">'
        f'{state_label}</span>'
        f'{progress_chip}</div>'
        f'<div class="app-sub">'
        f'<code>{match["original_name"]}</code> ↔ '
        f'<code>{match["translated_name"]}</code></div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with hdr_m:
    edited_chip = (
        f'<span class="ed" style="color:var(--accent-strong);">{edit_count} edited</span>'
        if edit_count else ''
    )
    conf_chip = ""
    if high_conf_count or low_conf_count:
        parts = []
        if high_conf_count:
            parts.append(f'<span style="color:#15803d;">{high_conf_count} hi-conf</span>')
        if low_conf_count:
            parts.append(f'<span style="color:#b45309;">{low_conf_count} lo-conf</span>')
        conf_chip = " · ".join(parts)
    st.markdown(
        f'''
        <div class="progress-shell">
          <div style="width:{cert_pct:.2f}%; background:var(--success);"></div>
          <div style="width:{flag_pct:.2f}%; background:var(--danger);"></div>
          <div style="width:{pend_pct:.2f}%; background:var(--ink-soft);"></div>
        </div>
        <div class="progress-meta">
          <span class="ok">{prog["certified"]} cert</span>
          <span class="fl">{prog["flagged"]} flag</span>
          <span class="cm">{prog["commented"]} cmt</span>
          <span class="pn">{pending_n} pending</span>
          {edited_chip}
          {conf_chip}
          <span style="margin-left:auto; color:#0a0a0a;"><b>{prog["certified"]}</b>/{prog["total"]}</span>
        </div>
        ''',
        unsafe_allow_html=True,
    )
with hdr_r:
    if is_locked:
        # Locked — show published timestamp + audit drawer entry point. No
        # write actions available from this header.
        if publication:
            pub_who = (publication["published_by"] or "?").split("@")[0]
            pub_when = publication["published_at"].strftime("%Y-%m-%d %H:%M") if publication.get("published_at") else ""
            st.markdown(
                f'<div style="text-align:right; font-size:11px; color:var(--ink-mute); '
                f'line-height:1.35; padding-top:4px;">'
                f'🔒 <b>Locked</b><br/>'
                f'{pub_when}<br/>'
                f'by {pub_who}'
                f'</div>',
                unsafe_allow_html=True,
            )
    elif is_promoting:
        st.warning("Promotion in flight — refresh in a moment.")
    else:
        # Active document — two-step CTA: first publish any pending edits to
        # the reviewed DOCX, then promote to gold.
        if edit_count:
            publish_btn_label = f"⤴ Publish · {edit_count} edit{'s' if edit_count != 1 else ''}"
            if st.button(
                publish_btn_label,
                key=f"open-pub-{pair_id}",
                type="primary",
                use_container_width=True,
                help=("Write a versioned reviewed copy of the translated DOCX to "
                      f"`{volume.REVIEWED_DIR}` with the {edit_count} pending edit(s) applied. "
                      "Required before gold-zone promotion."),
            ):
                st.session_state[f"show-pub-{pair_id}"] = True
        else:
            promote_disabled = not is_ready_for_gold
            promote_help = (
                "Promote to the golden zone. Once done this document is "
                "immutable; further edits require an administrator re-open."
                if is_ready_for_gold else f"Not yet: {gold_block_reason}"
            )
            if st.button(
                "🏅 Promote to Gold",
                key=f"open-promote-{pair_id}",
                type="primary" if is_ready_for_gold else "secondary",
                use_container_width=True,
                disabled=promote_disabled,
                help=promote_help,
            ):
                st.session_state[f"show-promote-{pair_id}"] = True
    if last_publish and not is_locked:
        st.caption(
            f"v{len(publish_history)} · {last_publish['edits_applied']} edits · "
            f"{last_publish['published_by'].split('@')[0]}"
        )


# ----------------------------------------------------------------------------
# Page bookkeeping (used inside the right rail)
# ----------------------------------------------------------------------------
total_pages = orig_paras[-1]["page"] if orig_paras else 1

page_first_idx: dict[int, int] = {}
for p in orig_paras:
    page_first_idx.setdefault(p["page"], p["idx"])

# ---------- Widget keys (single source of truth) ----------
PAGE_KEY      = f"page-{pair_id}"
ACTIVE_KEY    = f"para-{pair_id}"
COMPONENT_KEY = f"dp-{pair_id}"
NAV_KEY       = f"navigated-{pair_id}"  # True once user has interacted

# Initialize once per pair
if PAGE_KEY not in st.session_state:
    st.session_state[PAGE_KEY] = 1
if ACTIVE_KEY not in st.session_state:
    st.session_state[ACTIVE_KEY] = 0
if NAV_KEY not in st.session_state:
    st.session_state[NAV_KEY] = False


def _clamp_para(i: int) -> int:
    return max(0, min(total_paragraphs - 1, int(i)))


def _clamp_page(p: int) -> int:
    return max(1, min(total_pages, int(p)))


def _set_active_paragraph(idx: int):
    """Sync ACTIVE_KEY and PAGE_KEY together — must be called BEFORE widgets render
    (i.e. from a callback or top-of-script)."""
    idx = _clamp_para(idx)
    st.session_state[ACTIVE_KEY] = idx
    st.session_state[NAV_KEY] = True
    if 0 <= idx < len(orig_paras):
        st.session_state[PAGE_KEY] = orig_paras[idx]["page"]


def _set_page(p: int):
    p = _clamp_page(p)
    st.session_state[PAGE_KEY] = p
    st.session_state[ACTIVE_KEY] = page_first_idx.get(p, 0)
    st.session_state[NAV_KEY] = True


# ---------- Process pane events before any widgets render ----------
# Component value is stored in st.session_state[COMPONENT_KEY]; we use the
# event's `ts` to avoid reprocessing the same message across re-runs.
_pe = st.session_state.get(COMPONENT_KEY)
_last_ts = st.session_state.get(f"{COMPONENT_KEY}-last-ts", 0)
if isinstance(_pe, dict) and _pe.get("type") == "active":
    _ts = int(_pe.get("ts") or 0)
    if _ts > _last_ts:
        st.session_state[f"{COMPONENT_KEY}-last-ts"] = _ts
        _set_active_paragraph(int(_pe.get("idx", 0)))

# Now read the canonical state
active_idx   = int(st.session_state[ACTIVE_KEY])
current_page = int(st.session_state[PAGE_KEY])
has_navigated = bool(st.session_state.get(NAV_KEY, False))

# Recompute page paragraph metadata after sync
page_para_idxs = [p["idx"] for p in orig_paras if p["page"] == current_page]
page_certified = sum(
    1 for i in page_para_idxs
    if (feedback_map.get(i) or {}).get("status") == "certified"
)
page_total = len(page_para_idxs)


# ---------- Callbacks for nav buttons ----------
def cb_prev_page():
    _set_page(st.session_state[PAGE_KEY] - 1)

def cb_next_page():
    _set_page(st.session_state[PAGE_KEY] + 1)

def _next_review_target(start: int, direction: int) -> int:
    """Walk paragraphs in `direction` (±1) from `start`, returning the first
    index that's worth reviewer attention when "Skip hi-conf" is on:
      * not high-confidence, OR
      * not yet certified
    Falls back to plain +1/-1 if nothing matches (so the navigator still works
    at the document edges)."""
    if not st.session_state.get(f"skip-hiconf-{pair_id}", False):
        return start + direction
    cur = start + direction
    while 0 <= cur < total_paragraphs:
        conf = confidence_map.get(cur, {}).get("confidence", 0)
        status = (feedback_map.get(cur) or {}).get("status", "pending")
        if conf < HIGH_CONF_THRESHOLD or status != "certified":
            return cur
        cur += direction
    return start + direction  # nothing found — fall back to plain step

def cb_prev_para():
    _set_active_paragraph(_next_review_target(st.session_state[ACTIVE_KEY], -1))

def cb_next_para():
    _set_active_paragraph(_next_review_target(st.session_state[ACTIVE_KEY], +1))

def cb_page_input_changed():
    _set_page(int(st.session_state[PAGE_KEY]))

def cb_para_input_changed():
    _set_active_paragraph(int(st.session_state[ACTIVE_KEY]))

def _toast_locked():
    st.toast("Document is locked — promote was already completed.", icon="🔒")

def cb_certify_page():
    skip = st.session_state.get(f"skip-cmt-{pair_id}", True)
    advance = st.session_state.get(f"advance-{pair_id}", True)
    try:
        n = store.bulk_upsert_feedback(
            pair_id=pair_id,
            paragraph_idxs=page_para_idxs,
            status="certified",
            reviewer=auth.reviewer(),
            skip_commented=skip,
        )
    except store.PairLockedError:
        _toast_locked(); return
    st.toast(f"Certified {n} paragraph(s) on page {current_page}", icon="✅")
    if advance and current_page < total_pages:
        _set_page(current_page + 1)

def cb_certify_all():
    """One-click certify EVERY paragraph in the document. Skip-commented and
    skip-flagged are intentionally honored so reviewer-flagged items don't get
    silently swept up."""
    skip = st.session_state.get(f"skip-cmt-{pair_id}", True)
    all_idxs = [p["idx"] for p in orig_paras]
    # Don't bulldoze paragraphs the reviewer has actively flagged — those
    # need an explicit per-paragraph resolution.
    if skip:
        flagged_idxs = {
            i for i, fb in feedback_map.items()
            if fb.get("status") == "flagged"
        }
        all_idxs = [i for i in all_idxs if i not in flagged_idxs]
    try:
        n = store.bulk_upsert_feedback(
            pair_id=pair_id,
            paragraph_idxs=all_idxs,
            status="certified",
            reviewer=auth.reviewer(),
            skip_commented=skip,
        )
    except store.PairLockedError:
        _toast_locked(); return
    st.toast(f"Certified {n} paragraph(s) across the whole document", icon="✅")

def cb_set_status(new_status: str):
    cur = feedback_map.get(active_idx, {})
    try:
        store.upsert_feedback(pair_id, active_idx, new_status, cur.get("comment"), auth.reviewer())
    except store.PairLockedError:
        _toast_locked()

def cb_save_comment():
    cmt = st.session_state.get(f"comment-{pair_id}-{active_idx}", "")
    cur = feedback_map.get(active_idx, {})
    try:
        store.upsert_feedback(pair_id, active_idx, cur.get("status") or "pending", cmt, auth.reviewer())
        st.toast("Saved", icon="✅")
    except store.PairLockedError:
        _toast_locked()

def cb_revert_edit(idx: int):
    try:
        store.upsert_edit(pair_id, idx, None, auth.reviewer())
        st.toast(f"Reverted edit on paragraph {idx}", icon="🔄")
    except store.PairLockedError:
        _toast_locked()

def cb_save_edit():
    new_text = st.session_state.get(f"edit-{pair_id}-{active_idx}", "")
    original = tran_paras[active_idx]["text"] if active_idx < len(tran_paras) else ""
    cur = feedback_map.get(active_idx, {})
    if cur.get("edited_text") is None and new_text == original:
        st.toast("No change to save", icon="ℹ️")
        return
    try:
        result = store.upsert_edit(pair_id, active_idx, new_text, auth.reviewer())
    except store.PairLockedError:
        _toast_locked(); return
    if result.get("changed"):
        st.toast(f"Saved edit on paragraph {active_idx}", icon="✏️")
    else:
        st.toast("No change to save", icon="ℹ️")


@st.dialog("Publish edits to a reviewed copy")
def _publish_dialog():
    # Only edits that have NOT been baked into a prior publish are pending.
    pending_edits = store.get_pending_edits(pair_id)
    if not pending_edits:
        st.info("No pending edits to publish.")
        if st.button("Close"):
            st.session_state[f"show-pub-{pair_id}"] = False
            st.rerun()
        return

    version = store.next_publish_version(pair_id)
    target_path = volume.reviewed_path(pair_id, match["target_lang"], version)

    st.markdown(
        f"Apply **{len(pending_edits)}** edit(s) on top of the original "
        f"translated DOCX and write a new versioned file."
    )
    st.code(target_path, language=None)
    st.caption(
        "The pipeline output in `translated_inplace/` is never modified. "
        "Each publish creates a new file under `translated_reviewed/`. "
        "Edited paragraphs lose intra-paragraph mixed formatting (alignment, "
        "indentation and surrounding paragraphs are preserved)."
    )

    with st.expander(f"Preview: {len(pending_edits)} paragraph(s) being edited", expanded=False):
        for pidx in sorted(pending_edits):
            tran_p = tran_paras[pidx] if pidx < len(tran_paras) else None
            original_tran = (tran_p["text"] if tran_p else "") or "(empty)"
            edited = pending_edits[pidx] or "(empty)"
            st.markdown(
                f"**Paragraph {pidx}**  \n"
                f"<span style='color:#9ca3af'>was:</span> {original_tran}  \n"
                f"<span style='color:var(--accent-strong)'>now:</span> {edited}",
                unsafe_allow_html=True,
            )
            st.divider()

    bc, bp = st.columns(2)
    with bc:
        if st.button("Cancel", use_container_width=True):
            st.session_state[f"show-pub-{pair_id}"] = False
            st.rerun()
    with bp:
        if st.button(f"Publish v{version}", type="primary", use_container_width=True):
            # The published DOCX must contain ALL current edits, not just the
            # pending ones — otherwise v2 would silently lose v1's edits.
            # `pending_edits` is for "what's the diff vs last publish?" (and
            # what we display); `all_edits` is what we actually bake.
            all_edits = store.get_edits(pair_id)
            with st.spinner("Building edited DOCX and uploading…"):
                tran_bytes = volume.read_docx(match["translated_path"])
                new_bytes, applied = docx_render.apply_edits_to_docx(tran_bytes, all_edits)
                volume.upload_docx(target_path, new_bytes)
                store.record_publish(pair_id, target_path, applied, auth.reviewer())
            st.session_state[f"show-pub-{pair_id}"] = False
            st.toast(f"Published v{version} with {applied} edit(s) "
                     f"({len(pending_edits)} new since last publish)", icon="📤")
            st.rerun()


if st.session_state.get(f"show-pub-{pair_id}"):
    _publish_dialog()


@st.dialog("Promote to Golden Zone")
def _promote_dialog():
    ready, reason = store.is_ready_for_gold(pair_id)
    g_orig, g_tran = volume.golden_paths(pair_id, match["target_lang"])

    st.markdown(
        f"This action **freezes** `{pair_id}` and writes the certified "
        f"original + translation to the golden zone. After promotion:"
    )
    st.markdown(
        "- The document becomes read-only — no more status changes, edits, "
        "or comments through this app.\n"
        "- The audit trail is preserved and queryable; an administrator can "
        "re-open the document for re-review if needed.\n"
        "- The gold-zone copy is the system-of-record for downstream consumers."
    )

    st.markdown("**Golden paths:**")
    st.code(g_orig, language=None)
    st.code(g_tran, language=None)

    distinct_reviewers = store.get_distinct_reviewers(pair_id)
    st.markdown(f"**Reviewers on this document:** {', '.join(distinct_reviewers) if distinct_reviewers else '(none recorded)'}")

    if not ready:
        st.error(f"Not ready: {reason}")
        if st.button("Close"):
            st.session_state[f"show-promote-{pair_id}"] = False
            st.rerun()
        return

    confirm_text = st.text_input(
        f"Type the document id to confirm:",
        placeholder=pair_id,
        key=f"confirm-promote-{pair_id}",
    )
    bc, bp = st.columns(2)
    with bc:
        if st.button("Cancel", use_container_width=True):
            st.session_state[f"show-promote-{pair_id}"] = False
            st.rerun()
    with bp:
        confirmed = (confirm_text or "").strip() == pair_id
        if st.button(
            "🏅 Promote & Lock",
            type="primary",
            use_container_width=True,
            disabled=not confirmed,
            help=(None if confirmed else "Type the document id above to enable."),
        ):
            actor = auth.reviewer()
            # `st.status` gives a persistent, multi-step progress panel that
            # stays visible from click through `st.rerun()`. Avoids the
            # "spinner flashes then disappears while work continues" UX
            # we had with a single `st.spinner` wrap.
            with st.status("Promoting to golden zone…", expanded=True) as status:
                result = None
                delta_status_msg = None
                try:
                    # 1. State change UNDER_REVIEW → PROMOTING (audited)
                    status.write("• Locking review state…")
                    store.begin_gold_promotion(pair_id, actor)

                    try:
                        # 2. Read source files. Use the latest reviewed copy
                        #    as the translated golden source if there's been a publish.
                        status.write("• Reading source files from Volume…")
                        orig_bytes = volume.read_docx(match["original_path"])
                        if publish_history:
                            tran_source_path = publish_history[0]["output_path"]
                        else:
                            tran_source_path = match["translated_path"]
                        tran_bytes = volume.read_docx(tran_source_path)

                        # 3. Hash + copy to golden zone
                        status.write("• Hashing + copying to golden Volume…")
                        copy_info = volume.copy_to_golden(
                            pair_id=pair_id,
                            target_lang=match["target_lang"] or "tr",
                            original_bytes=orig_bytes,
                            translated_bytes=tran_bytes,
                        )

                        # 4. Complete: golden_publications row + lock
                        status.write("• Writing publication record + locking…")
                        result = store.complete_gold_promotion(
                            pair_id=pair_id,
                            actor=actor,
                            golden_original_path=copy_info["golden_original_path"],
                            golden_translated_path=copy_info["golden_translated_path"],
                            golden_original_hash=copy_info["golden_original_hash"],
                            golden_translated_hash=copy_info["golden_translated_hash"],
                            total_paragraphs=prog["total"],
                            certified_paragraphs=prog["certified"],
                            edits_applied=sum(p["edits_applied"] for p in publish_history),
                            distinct_reviewers=distinct_reviewers,
                        )
                    except Exception as inner:
                        try:
                            store.abort_gold_promotion(pair_id, actor, str(inner))
                        except Exception:
                            pass
                        raise

                    # 5. Delta sync (fail-soft) — STILL inside the status block,
                    #    so the user sees us doing work right up until rerun.
                    status.write("• Mirroring audit + publication to Delta…")
                    try:
                        sync_result = delta_sync.sync_pair_to_delta(
                            pair_id, publication_id=result["publication_id"]
                        )
                        if sync_result.get("skipped"):
                            delta_status_msg = "Delta sync skipped (no warehouse configured)."
                        else:
                            c = sync_result["counts"]
                            delta_status_msg = (
                                f"Delta mirror: {c['audit_events']} events, "
                                f"{c['golden_publications']} pub, "
                                f"{c['silver_review_snapshots']} para snapshot"
                            )
                    except Exception as sync_err:
                        delta_status_msg = (
                            f"⚠ Delta sync deferred — promotion is complete in Lakebase. "
                            f"Re-run by an admin to mirror to Delta. ({sync_err})"
                        )
                        try:
                            from server.db import pool as _pool
                            with _pool.connection() as _c:
                                with _c.cursor() as _cur:
                                    store._emit_audit(
                                        _cur, pair_id=pair_id,
                                        event_type="DELTA_SYNC_FAILED",
                                        actor=actor,
                                        after={"error": str(sync_err)},
                                        correlation_id=f"publication:{result['publication_id']}",
                                    )
                                _c.commit()
                        except Exception:
                            pass

                    status.update(
                        label=f"✅ Promoted · publication #{result['publication_id']}",
                        state="complete", expanded=False,
                    )
                except Exception as e:
                    status.update(label=f"Promotion failed: {e}", state="error")
                    # Don't rerun on failure — let the user see the error in-dialog.
                    st.stop()

            # Success path: close dialog, surface the delta-status banner, refresh sidebar.
            st.session_state[f"show-promote-{pair_id}"] = False
            st.toast(
                f"Promoted to gold · publication #{result['publication_id']}",
                icon="🏅",
            )
            if delta_status_msg:
                st.session_state["last_delta_msg"] = delta_status_msg
            list_volume_cached.clear()
            st.rerun()


if st.session_state.get(f"show-promote-{pair_id}"):
    _promote_dialog()


# ----------------------------------------------------------------------------
# Two-column layout: dual-pane viewer | review rail
# ----------------------------------------------------------------------------
viewer_col, rail_col = st.columns([5.2, 1.6], gap="medium")

with viewer_col:
    dual_pane(
        orig_html=orig_html,
        tran_html=tran_html,
        orig_paragraphs=orig_paras,
        tran_paragraphs=tran_paras,
        feedback={
            int(idx): {
                "status":  row["status"],
                "comment": row.get("comment") or "",
            } for idx, row in feedback_map.items()
        },
        active_idx=active_idx if has_navigated else None,
        source_lang=source_lang,
        target_lang=match["target_lang"] or "tr",
        height=880,
        key=COMPONENT_KEY,
    )

def _rail_label(text: str, top_margin: int = 6):
    # top_margin kept for API compatibility — visual spacing is in .rail-section-label CSS
    st.markdown(f'<div class="rail-section-label">{text}</div>', unsafe_allow_html=True)


with rail_col:
    st.markdown('<div class="review-rail">', unsafe_allow_html=True)

    if is_locked and publication:
        pub_who = (publication["published_by"] or "?").split("@")[0]
        pub_when = publication["published_at"].strftime("%Y-%m-%d %H:%M") if publication.get("published_at") else ""
        st.markdown(
            f'<div class="locked-banner">'
            f'<span class="glyph">🔒</span>'
            f'<div><b>Published to gold</b> on {pub_when} by {pub_who}.<br/>'
            f'Review is read-only. Audit trail is preserved in the sidebar drawer.</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ---- Page-level bulk action ----
    _rail_label("Page", top_margin=2)
    pn_l, pn_m, pn_r = st.columns([1, 2, 1])
    with pn_l:
        st.button("◀", key=f"prev-page-btn-{pair_id}", use_container_width=True,
                  disabled=current_page <= 1, help="Previous page", on_click=cb_prev_page)
    with pn_m:
        st.number_input(
            "page",
            min_value=1,
            max_value=max(1, total_pages),
            label_visibility="collapsed",
            key=PAGE_KEY,
            on_change=cb_page_input_changed,
        )
    with pn_r:
        st.button("▶", key=f"next-page-btn-{pair_id}", use_container_width=True,
                  disabled=current_page >= total_pages, help="Next page", on_click=cb_next_page)

    st.markdown(
        f'<div style="font-size:11px; color:#6b7280; margin:0 0 6px 0; '
        f'font-variant-numeric:tabular-nums; line-height:1.2;">'
        f'page {current_page} / {total_pages} · '
        f'<span style="color:#10b981; font-weight:600;">{page_certified}</span>'
        f'<span style="color:#9ca3af;">/{page_total}</span> ✓ on this page</div>',
        unsafe_allow_html=True,
    )

    cb_l, cb_r = st.columns([1, 1], gap="small")
    with cb_l:
        st.checkbox(
            "Skip cmt",
            value=True,
            key=f"skip-cmt-{pair_id}",
            help="Leave paragraphs with comments alone when bulk-certifying.",
        )
    with cb_r:
        st.checkbox(
            "Auto-next",
            value=True,
            key=f"advance-{pair_id}",
            help="Move to next page after certifying.",
        )
    # Reviewer-attention filter. When on, the ← → paragraph nav arrows skip
    # paragraphs that are both high-confidence (≥ 0.9) AND already certified —
    # so the reviewer's clicks only land on things that need attention.
    st.checkbox(
        f"Skip hi-conf (≥ {HIGH_CONF_THRESHOLD:.1f})",
        value=False,
        key=f"skip-hiconf-{pair_id}",
        help=(
            "When stepping with ← / → arrows, skip paragraphs whose heuristic "
            "confidence is high AND status is already certified. Lets you "
            "focus only on the ones that need eyes. The confidence score is "
            "a triage hint based on length-ratio, source-script leakage, and "
            "repetition — not a quality guarantee."
        ),
    )
    st.button(
        f"✓ Certify page · {page_total} paragraphs",
        type="primary",
        use_container_width=True,
        disabled=page_total == 0 or is_locked,
        key=f"cert-page-btn-{pair_id}",
        on_click=cb_certify_page,
    )
    # Certify the entire document in one click. Honors "Skip cmt" and never
    # touches flagged paragraphs — those require explicit resolution.
    remaining = max(0, prog["total"] - prog["certified"])
    st.button(
        f"✓✓ Certify whole doc · {remaining} remaining",
        use_container_width=True,
        disabled=remaining == 0 or is_locked,
        key=f"cert-all-btn-{pair_id}",
        on_click=cb_certify_all,
        help=(
            "Bulk-certify every paragraph in the document. Flagged paragraphs "
            "are skipped (resolve them individually first). When 'Skip cmt' is "
            "on, paragraphs with comments are also left untouched."
        ),
    )

    _rail_label("Paragraph")

    nav_prev, nav_idx, nav_next = st.columns([1, 2, 1])
    with nav_prev:
        st.button("←", key=f"prev-para-btn-{pair_id}", use_container_width=True,
                  disabled=active_idx <= 0, on_click=cb_prev_para)
    with nav_idx:
        st.number_input(
            "Paragraph",
            min_value=0,
            max_value=max(0, total_paragraphs - 1),
            label_visibility="collapsed",
            key=ACTIVE_KEY,
            on_change=cb_para_input_changed,
        )
    with nav_next:
        st.button("→", key=f"next-para-btn-{pair_id}", use_container_width=True,
                  disabled=active_idx >= total_paragraphs - 1, on_click=cb_next_para)

    if has_navigated:
        # Editor for the active paragraph
        orig_p = orig_paras[active_idx] if active_idx < len(orig_paras) else None
        tran_p = tran_paras[active_idx] if active_idx < len(tran_paras) else None
        current = feedback_map.get(active_idx, {})

        empty_html = '<em style="color:var(--ink-soft)">(empty)</em>'
        orig_text = (orig_p["text"] if orig_p else "") or empty_html
        tran_text = (tran_p["text"] if tran_p else "") or empty_html

        st.markdown(
            f'<div class="rail-section-label">Original ({source_lang_chip})</div>'
            f'<div class="para-text">{orig_text}</div>',
            unsafe_allow_html=True,
        )

        # Editable translation. Prefilled with the live edit overlay if there
        # is one, otherwise the original translated text. Save writes to the
        # `edited_text` column and appends a row to review_edit_history; the
        # header Publish button bundles all current edits into a versioned
        # DOCX in `translated_reviewed/`.
        existing_edit = current.get("edited_text")
        edit_default = existing_edit if existing_edit is not None else (tran_p["text"] if tran_p else "")
        edit_widget_key = f"edit-{pair_id}-{active_idx}"
        edited_pill = (
            ' <span style="font-size:9px; font-weight:600; letter-spacing:0.1em; '
            'text-transform:uppercase; color:var(--accent-strong); '
            'background:var(--accent-soft); padding:1px 5px; border-radius:2px;">edited</span>'
            if existing_edit is not None else ''
        )
        # Confidence pill — green/amber/red based on heuristic score for this paragraph.
        conf_info = confidence_map.get(active_idx)
        conf_pill = ""
        if conf_info is not None:
            c = conf_info["confidence"]
            if c >= HIGH_CONF_THRESHOLD:
                bg, fg, label = "#dcfce7", "#15803d", f"conf {c:.2f}"
            elif c >= LOW_CONF_THRESHOLD:
                bg, fg, label = "#fef3c7", "#b45309", f"conf {c:.2f}"
            else:
                bg, fg, label = "#fee2e2", "#b91c1c", f"conf {c:.2f}"
            tip_bits = []
            if conf_info.get("length_ratio") is not None:
                tip_bits.append(f"ratio={conf_info['length_ratio']:.2f}")
            up = conf_info.get("untranslated_pct") or 0
            if up:
                tip_bits.append(f"untrans={up:.1%}")
            rn = conf_info.get("repeated_ngrams") or 0
            if rn:
                tip_bits.append(f"loops={rn}")
            tip = " · ".join(tip_bits) or "no red flags"
            conf_pill = (
                f' <span title="{tip}" style="font-size:9px; font-weight:600; '
                f'letter-spacing:0.1em; text-transform:uppercase; color:{fg}; '
                f'background:{bg}; padding:1px 5px; border-radius:2px;">{label}</span>'
            )
        st.markdown(
            f'<div class="rail-section-label">Translated ({target_lang_chip}){edited_pill}{conf_pill}</div>',
            unsafe_allow_html=True,
        )
        st.text_area(
            "Translated text",
            value=edit_default,
            label_visibility="collapsed",
            height=120,
            key=edit_widget_key,
            placeholder="Edit the translation here…",
            disabled=is_locked,
        )
        # Detect unsaved changes by comparing the live widget value to whatever
        # is currently persisted (the edit overlay if present, else the original
        # translation). The widget value is only present after first render.
        current_text = st.session_state.get(edit_widget_key, edit_default)
        has_unsaved_change = current_text != edit_default

        ec_l, ec_r = st.columns([1, 1])
        with ec_l:
            st.button(
                "✏️ Save edit" if has_unsaved_change else "✓ Saved",
                type="primary" if has_unsaved_change else "secondary",
                use_container_width=True,
                disabled=not has_unsaved_change or is_locked,
                key=f"save-edit-{pair_id}-{active_idx}",
                on_click=cb_save_edit,
                help=("Persist this edit to the database (auditable). The header "
                      "Publish button is what writes it back to the DOCX."
                      if has_unsaved_change else "No changes since last save."),
            )
        with ec_r:
            st.button(
                "↺ Revert",
                use_container_width=True,
                disabled=existing_edit is None or is_locked,
                key=f"revert-edit-{pair_id}-{active_idx}",
                on_click=cb_revert_edit, args=(active_idx,),
                help=("Drop this paragraph's edit and fall back to the original "
                      "translation." if existing_edit is not None
                      else "Nothing to revert — no edit on this paragraph yet."),
            )

        st.markdown(
            f'<div class="rail-section-label">Status</div>',
            unsafe_allow_html=True,
        )
        s = current.get("status", "pending")
        bp, bc, bf = st.columns(3)
        with bp:
            st.button("● Pending", key=f"st-pend-{pair_id}", use_container_width=True,
                      type="primary" if s == "pending" else "secondary",
                      disabled=is_locked,
                      on_click=cb_set_status, args=("pending",))
        with bc:
            st.button("✓ Certify", key=f"st-cert-{pair_id}", use_container_width=True,
                      type="primary" if s == "certified" else "secondary",
                      disabled=is_locked,
                      on_click=cb_set_status, args=("certified",))
        with bf:
            st.button("⚑ Flag", key=f"st-flag-{pair_id}", use_container_width=True,
                      type="primary" if s == "flagged" else "secondary",
                      disabled=is_locked,
                      on_click=cb_set_status, args=("flagged",))

        st.text_area(
            "Comment",
            value=current.get("comment") or "",
            label_visibility="visible",
            height=75,
            key=f"comment-{pair_id}-{active_idx}",
            placeholder="Notes for this paragraph…",
            disabled=is_locked,
        )
        save_col, _ = st.columns([0.8, 2.2])
        with save_col:
            st.button("Save", type="primary", use_container_width=True,
                      disabled=is_locked,
                      key=f"save-cmt-{pair_id}-{active_idx}", on_click=cb_save_comment)

        if current.get("reviewer"):
            st.caption(f"Last edited by {current['reviewer']}")
    else:
        # No paragraph selected yet — show a quiet hint
        st.markdown(
            '<div class="rail-empty">'
            '<div class="rail-empty-glyph">📄</div>'
            '<div class="rail-empty-title">No paragraph selected</div>'
            '<div class="rail-empty-help">Click any paragraph in either pane '
            'to focus it for review, or use the navigator above.</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    # Always-visible legend at the bottom of the rail
    st.markdown(
        '<div class="rail-legend">'
        '<div class="rail-section-label">Legend</div>'
        '<div class="legend-grid">'
        '  <div class="legend-row"><span class="dot pending"></span>Pending</div>'
        '  <div class="legend-row"><span class="dot certified"></span>Certified</div>'
        '  <div class="legend-row"><span class="dot flagged"></span>Flagged</div>'
        '  <div class="legend-row"><span class="dot commented"></span>Has comment</div>'
        '</div>'
        '<div class="legend-tip">'
        'Hover a paragraph to highlight its match in the other pane. '
        'Click to focus it for review.'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("</div>", unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Issues — compact list in the sidebar (jumps to paragraph on click)
# ----------------------------------------------------------------------------
with st.sidebar:
    issue_count = prog["flagged"] + prog["commented"]
    with st.expander(f"⚑ Issues ({prog['flagged']} flag · {prog['commented']} cmt)", expanded=False):
        any_row = False
        for i in range(total_paragraphs):
            fb = feedback_map.get(i)
            if not fb:
                continue
            if fb["status"] != "flagged" and not (fb.get("comment") or "").strip():
                continue
            any_row = True
            icon = "⚑" if fb["status"] == "flagged" else "✎"
            preview = (orig_paras[i]["text"] if i < len(orig_paras) else "")[:48]
            label = f"{icon} para {i}  {preview}"
            st.button(
                label, key=f"issue-jump-{pair_id}-{i}",
                use_container_width=True,
                on_click=_set_active_paragraph, args=(i,),
            )
        if not any_row:
            st.caption("No flagged or commented paragraphs yet.")

    with st.expander(f"✎ Edit history ({edit_count} live)", expanded=False):
        history = store.list_edit_history(pair_id, limit=100)
        if not history:
            st.caption("No edits yet.")
        else:
            for h in history[:25]:
                who = (h.get("reviewer") or "?").split("@")[0]
                when = h["edited_at"].strftime("%m-%d %H:%M") if h.get("edited_at") else ""
                action = "↺" if h["new_text"] is None else "✎"
                preview = (h["new_text"] or h["previous_text"] or "")[:32]
                st.button(
                    f"{action} para {h['paragraph_idx']} · {who} · {when} — {preview}",
                    key=f"hist-jump-{pair_id}-{h['edit_id']}",
                    use_container_width=True,
                    on_click=_set_active_paragraph,
                    args=(h["paragraph_idx"],),
                )
            if len(history) > 25:
                st.caption(f"+ {len(history) - 25} older entr{'ies' if len(history) - 25 > 1 else 'y'}")

    if publish_history:
        with st.expander(f"⤴ Published versions ({len(publish_history)})", expanded=False):
            for p in publish_history:
                who = (p.get("published_by") or "?").split("@")[0]
                when = p["published_at"].strftime("%m-%d %H:%M") if p.get("published_at") else ""
                st.markdown(
                    f"**v{len(publish_history) - publish_history.index(p)}** · "
                    f"{p['edits_applied']} edits · {who} · {when}",
                )
                st.code(p["output_path"], language=None)

    if publication:
        with st.expander("🏅 Golden publication", expanded=False):
            pub_who = (publication["published_by"] or "?").split("@")[0]
            pub_when = publication["published_at"].strftime("%Y-%m-%d %H:%M") if publication.get("published_at") else ""
            st.markdown(f"**Published** by {pub_who} on {pub_when}")
            st.markdown(
                f"- {publication['certified_paragraphs']}/{publication['total_paragraphs']} "
                f"paragraphs certified\n"
                f"- {publication['edits_applied']} edits applied\n"
                f"- reviewers: {', '.join(publication.get('distinct_reviewers') or [])}"
            )
            st.markdown("**Golden original**")
            st.code(publication["golden_original_path"], language=None)
            st.caption(f"SHA-256: `{publication['golden_original_hash']}`")
            st.markdown("**Golden translated**")
            st.code(publication["golden_translated_path"], language=None)
            st.caption(f"SHA-256: `{publication['golden_translated_hash']}`")
            # Delta mirror status — separate row, lets ops see at-a-glance
            # whether long-term archive is consistent with Lakebase.
            if publication.get("delta_synced_at"):
                synced = publication["delta_synced_at"].strftime("%Y-%m-%d %H:%M")
                st.success(f"📦 Delta mirror synced at {synced}")
                st.caption(
                    f"`{delta_sync.DELTA_FQN}.audit_events` · "
                    f"`{delta_sync.DELTA_FQN}.golden_publications` · "
                    f"`{delta_sync.DELTA_FQN}.silver_review_snapshots`"
                )
            else:
                if delta_sync.enabled():
                    st.warning("📦 Delta mirror pending — an admin can re-run sync.")
                else:
                    st.caption("Delta mirror not configured for this app.")

    # Glossary expander: shows mined (model_phrase → correction) patterns the
    # platform has learned from past reviewer edits. Phase 1c will inject these
    # into the translation prompt; for now we just surface what's been learned.
    src_for_gloss = (source_lang or "").lower() or None
    tgt_for_gloss = (match.get("target_lang") or "").lower() or None
    gloss_entries = []
    try:
        gloss_entries = glossary_mod.list_glossary(
            source_lang=src_for_gloss, target_lang=tgt_for_gloss, limit=30
        )
    except Exception:
        pass
    with st.expander(f"📚 Glossary ({len(gloss_entries)} learned)", expanded=False):
        st.caption(
            "Patterns mined from reviewer edits across documents. The platform "
            "will eventually inject these into future translation prompts so "
            "the same correction doesn't have to be made twice."
        )
        if st.button("↻ Refresh from edit history",
                     key=f"refresh-gloss-{pair_id}",
                     help="Re-scan review_edit_history and upsert new entries."):
            try:
                n = glossary_mod.mine_glossary()
                st.toast(f"Glossary mined — {n} entry/entries touched", icon="📚")
                st.rerun()
            except Exception as e:
                st.error(f"Mining failed: {e}")
        if not gloss_entries:
            st.caption("Nothing learned yet. Reviewers need to make repeated edits "
                       "(same model output → same correction) before patterns surface.")
        else:
            for g in gloss_entries[:30]:
                badge = "✓" if g["approved"] else "○"
                st.markdown(
                    f"<div style='font-size:11px; line-height:1.4; "
                    f"padding:4px 0; border-bottom:1px solid var(--rule-soft);'>"
                    f"<span style='color:var(--ink-mute); font-family:var(--mono);'>"
                    f"{badge} ×{g['occurrences']} · {g['distinct_reviewers']} reviewer(s)</span><br/>"
                    f"<span style='color:var(--ink-mute); text-decoration:line-through;'>"
                    f"{(g['model_phrase'] or '')[:90]}</span><br/>"
                    f"<span style='color:var(--accent-strong);'>→ "
                    f"{(g['correction'] or '')[:90]}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    with st.expander("📜 Audit trail", expanded=False):
        events = store.list_audit_events(pair_id, limit=200)
        if not events:
            st.caption("No events yet.")
        else:
            for e in events[:50]:
                who = (e.get("actor") or "?").split("@")[0]
                when = e["event_at"].strftime("%m-%d %H:%M:%S") if e.get("event_at") else ""
                et = e["event_type"]
                pidx = e.get("paragraph_idx")
                pidx_part = f" para {pidx}" if pidx is not None else ""
                # Render before/after compactly for the common diff cases.
                detail = ""
                before = e.get("before_value") or {}
                after  = e.get("after_value") or {}
                if et == "PARAGRAPH_STATUS_CHANGED":
                    detail = f" {before.get('status') or '∅'} → {after.get('status')}"
                elif et == "PARAGRAPH_EDITED":
                    new_t = (after.get("text") or "")[:32]
                    detail = f" — {new_t}"
                elif et == "PARAGRAPH_REVERTED":
                    detail = " — reverted"
                elif et == "PARAGRAPH_COMMENT_SET":
                    detail = f" — {(after.get('comment') or '')[:32]}"
                elif et == "BULK_STATUS_CHANGED":
                    detail = f" — {after.get('status')} × {after.get('rows_affected', 0)}"
                elif et == "REVIEWED_DOCX_PUBLISHED":
                    detail = f" — v? · {after.get('edits_applied', 0)} edits"
                elif et == "GOLD_PROMOTED":
                    detail = f" — pub#{after.get('publication_id')}"
                elif et == "INVALID_WRITE_BLOCKED":
                    detail = f" — {after.get('blocked_event', '?')}"
                st.markdown(
                    f"<div style='font-size:11px; line-height:1.35; "
                    f"font-family:var(--mono); color:var(--ink-2); "
                    f"padding:3px 0; border-bottom:1px solid var(--rule-soft);'>"
                    f"<b>{when}</b> · <span style='color:var(--accent-strong)'>{et}</span>"
                    f"{pidx_part} · <i>{who}</i>{detail}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            if len(events) > 50:
                st.caption(f"+ {len(events) - 50} older event(s)")
