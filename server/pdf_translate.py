"""In-app PDF parse + translate pipeline — the PDF workflow's analogue of the
DOCX translation job (`setup/docx_inplace_translation.py`).

Per the hybrid design, DOCX stays on the file-arrival Lakeflow job; PDF is
handled here, inside the Databricks App, as a background task kicked off on
upload. The stages:

  1. `ai_parse_document` (SQL on the app's SQL warehouse) → structured elements
     (title / section_header / text / table / figure / caption), each with a
     reading-order `id`, a `page_id`, and a `bbox`.
  2. Translate each element's text via the Foundation Model API (+ glossary
     injection, read straight from Lakebase — the app can reach it, unlike the
     job which needs the Delta mirror).
  3. Persist a translation artifact JSON (`{stem}_translated_{slug}.pdf.json`)
     that `server/pdf_render.py` renders into the SAME review contract a DOCX
     produces — so the review UI can't tell the two apart.

The warehouse executor (`delta_sync._execute`) and glossary reader
(`glossary.glossary_for_prompt`) are reused as-is.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import escape

from . import config, volume, delta_sync
from . import glossary as glossary_mod
from . import docx_render

log = logging.getLogger("doc_translation.pdf")

PARSE_TIMEOUT_S = 300.0
MAX_WORKERS = 8
MAX_GLOSSARY_INJECT = 20
MAX_TOKENS = 8192

_TABLE_CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# Defensive cleanup of model output: leading ATX markdown header (`# `), and a
# single pair of wrapping quotes. `ai_parse` already gives us the element TYPE,
# so we style headings ourselves — the model must not add markdown or quotes.
_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s+")

# Process-local translation cache (language|text -> translation). Bounds cost of
# repeated boilerplate within and across documents in one app instance.
_TRANSLATE_CACHE: dict[str, str] = {}


def slug_for(target_lang: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "_", (target_lang or "translated").lower()).strip("_")
        or "translated"
    )


def artifact_path_for(pdf_path: str, target_lang: str) -> str:
    """`raw_documents/Foo.pdf` → `translated_inplace/Foo_translated_english.pdf.json`."""
    stem = pdf_path.rsplit("/", 1)[-1]
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return f"{config.TRANSLATED_DIR}/{stem}_translated_{slug_for(target_lang)}.pdf.json"


# ---------------------------------------------------------------------------
# Stage 1 — parse
# ---------------------------------------------------------------------------

def parse_pdf(pdf_path: str) -> list[dict]:
    """Run `ai_parse_document` on the warehouse and return the elements array.

    Each element: {id, type, content, bbox:[{coord:[x1,y1,x2,y2], page_id}], …}.
    """
    stmt = (
        "SELECT to_json(ai_parse_document(content, map('version','2.0'))"
        ":document:elements) AS els "
        f"FROM read_files({delta_sync._esc(pdf_path)}, format => 'binaryFile')"
    )
    out = delta_sync._execute(stmt, timeout_s=PARSE_TIMEOUT_S)
    data = (out.get("result") or {}).get("data_array") or []
    if not data or not data[0] or data[0][0] is None:
        raise RuntimeError("ai_parse_document returned no elements for " + pdf_path)
    return json.loads(data[0][0])


# ---------------------------------------------------------------------------
# Stage 2 — translate
# ---------------------------------------------------------------------------

def _is_translatable(text: str) -> bool:
    return bool(text) and any(c.isalpha() for c in text)


def _clean_translation(out: str, source: str) -> str:
    """Strip stray markdown-header markers and a single pair of wrapping quotes
    the model sometimes adds. Never let cleanup empty out a non-empty result —
    fall back to the source if it does."""
    if not out:
        return out
    cleaned = _MD_HEADER_RE.sub("", out).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'", "“", "「"):
        inner = cleaned[1:-1].strip()
        if inner:
            cleaned = inner
    return cleaned or source


def _fmapi_chat(model_endpoint: str, system: str, user: str) -> str:
    """Call an FMAPI chat endpoint via the SDK REST client (version-robust —
    same api_client.do pattern used elsewhere). Returns the assistant text."""
    resp = config.w().api_client.do(
        "POST",
        f"/serving-endpoints/{model_endpoint}/invocations",
        body={
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": MAX_TOKENS,
        },
    )
    choices = (resp or {}).get("choices") or []
    if not choices:
        raise RuntimeError(f"FMAPI returned no choices: {str(resp)[:200]}")
    return (choices[0].get("message", {}).get("content") or "").strip()


def _system_prompt(base: str, target_lang: str,
                   glossary_pairs: list[tuple[str, str]], text: str) -> str:
    """Base prompt with `{lang}` filled, plus glossary rules for any source
    terms that actually occur in this segment (mirrors the DOCX path's
    retrieval-based injection, minus the Aho-Corasick dependency — the glossary
    is small and capped, so a substring scan is fine)."""
    prompt = base.replace("{lang}", target_lang)
    matches = [(mp, corr) for mp, corr in glossary_pairs if mp and mp in text]
    matches = matches[:MAX_GLOSSARY_INJECT]
    if matches:
        lines = "\n".join(f'- "{mp}" -> "{corr}"' for mp, corr in matches)
        prompt += (
            "\n\nGLOSSARY — when the source contains the following terms, use the "
            "specified translation verbatim:\n" + lines
        )
    return prompt


def _translate_text(text: str, base_prompt: str, target_lang: str,
                    glossary_pairs: list[tuple[str, str]], model_endpoint: str) -> str:
    if not text or not text.strip():
        return text
    key = f"{target_lang}|{text}"
    cached = _TRANSLATE_CACHE.get(key)
    if cached is not None:
        return cached
    system = _system_prompt(base_prompt, target_lang, glossary_pairs, text)
    try:
        out = _clean_translation(_fmapi_chat(model_endpoint, system, text), text) or text
    except Exception:
        log.exception("pdf translate: segment failed; keeping source text")
        out = text
    _TRANSLATE_CACHE[key] = out
    return out


_TABLE_ADDENDUM = (
    "\n\nThe input is an HTML <table>. Translate ONLY the human-readable text "
    "inside cells to {lang}. Preserve every HTML tag, attribute, number, and the "
    "table structure EXACTLY. Use the surrounding cells for context. Return ONLY "
    "the resulting HTML table — no commentary, no code fences."
)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _translate_table(html: str, base_prompt: str, target_lang: str,
                     glossary_pairs: list[tuple[str, str]], model_endpoint: str) -> str:
    """Translate a whole table in one FMAPI call so the model has cross-cell
    context (an isolated cell like `相` is ambiguous, but resolves to `Phase`
    beside `第II相`). Falls back to per-cell translation if the model returns
    something that isn't a table."""
    if not html or "<t" not in html.lower():
        return html
    key = f"{target_lang}|TABLE|{html}"
    cached = _TRANSLATE_CACHE.get(key)
    if cached is not None:
        return cached
    system = _system_prompt(base_prompt, target_lang, glossary_pairs, html) + \
        _TABLE_ADDENDUM.replace("{lang}", target_lang)
    try:
        out = _FENCE_RE.sub("", _fmapi_chat(model_endpoint, system, html).strip()).strip()
        if "<table" not in out.lower():
            raise ValueError("model did not return an HTML table")
    except Exception:
        log.warning("pdf translate: whole-table translate failed; per-cell fallback", exc_info=True)
        out = _translate_table_cellwise(html, base_prompt, target_lang, glossary_pairs, model_endpoint)
    _TRANSLATE_CACHE[key] = out
    return out


def _translate_table_cellwise(html: str, base_prompt: str, target_lang: str,
                              glossary_pairs: list[tuple[str, str]], model_endpoint: str) -> str:
    """Per-cell fallback: translate each cell's text independently, preserving
    the surrounding tags."""
    def repl(m: re.Match) -> str:
        open_t, inner, close_t = m.group(1), m.group(2), m.group(3)
        cell_text = _TAG_RE.sub("", inner).strip()
        if not cell_text:
            return m.group(0)
        tr = _translate_text(cell_text, base_prompt, target_lang, glossary_pairs, model_endpoint)
        return f"{open_t}{escape(tr)}{close_t}"

    return _TABLE_CELL_RE.sub(repl, html or "")


def _element_source(el: dict) -> str:
    return el.get("content") or ""


def _element_page(el: dict) -> int:
    bbox = (el.get("bbox") or [{}])
    return int((bbox[0].get("page_id") or 0)) + 1 if bbox else 1


def _element_coord(el: dict):
    bbox = el.get("bbox") or []
    return bbox[0].get("coord") if bbox else None


def translate_pdf(pdf_path: str, *, target_lang: str, base_prompt: str,
                  model_endpoint: str, source_lang: str | None = None) -> dict:
    """Full parse + translate → translation artifact dict (not yet persisted)."""
    raw_elements = parse_pdf(pdf_path)

    src_lang = (source_lang or "").strip() or docx_render.detect_lang(
        [{"text": _element_source(e)} for e in raw_elements]
    )
    try:
        glossary_pairs = glossary_mod.glossary_for_prompt(
            source_lang=src_lang, target_lang=(target_lang or "").lower(), top_n=200
        )
    except Exception:
        log.warning("pdf translate: glossary lookup failed; continuing without", exc_info=True)
        glossary_pairs = []

    def _translate_one(el: dict) -> dict:
        etype = el.get("type", "text")
        src = _element_source(el)
        if etype == "table":
            tgt = _translate_table(src, base_prompt, target_lang, glossary_pairs, model_endpoint)
        elif _is_translatable(src):
            tgt = _translate_text(src, base_prompt, target_lang, glossary_pairs, model_endpoint)
        else:
            tgt = src
        return {
            "id": int(el["id"]),
            "type": etype,
            "page": _element_page(el),
            "bbox": _element_coord(el),
            "source": src,
            "target": tgt,
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        elements = list(ex.map(_translate_one, raw_elements))
    elements.sort(key=lambda e: e["id"])

    return {
        "source_lang": src_lang,
        "target_lang": target_lang,
        "pages": max((e["page"] for e in elements), default=1),
        "model_endpoint": model_endpoint,
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(pdf_path: str, *, target_lang: str, base_prompt: str, model_endpoint: str) -> str:
    """Parse + translate + persist the artifact. Returns the artifact path."""
    artifact = translate_pdf(
        pdf_path, target_lang=target_lang, base_prompt=base_prompt,
        model_endpoint=model_endpoint,
    )
    ap = artifact_path_for(pdf_path, target_lang)
    volume.upload_docx(ap, json.dumps(artifact, ensure_ascii=False).encode("utf-8"))
    log.info("pdf translate: wrote artifact %s (%d elements)", ap, len(artifact["elements"]))
    return ap
