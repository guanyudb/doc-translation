# Databricks notebook source
# MAGIC %md
# MAGIC # In-place PDF Translation (durable job)
# MAGIC
# MAGIC The PDF analogue of `docx_inplace_translation.py`. Called once per unpaired
# MAGIC `.pdf` by the auto-translate watcher via `dbutils.notebook.run`. This replaces
# MAGIC the app's in-process daemon thread (`server/pdf_translate.py`) with a durable,
# MAGIC retryable job step whose status lives in `bronze_documents`.
# MAGIC
# MAGIC 1. `ai_parse_document` (SQL on the configured warehouse) → structured elements
# MAGIC    (title/section_header/text/table/figure/caption), each with `id`, `page_id`, `bbox`.
# MAGIC 2. Translate each element via the Foundation Model API or a UC AI Gateway
# MAGIC    endpoint (+ approved-glossary injection read from the Delta mirror).
# MAGIC 3. Write the artifact `{stem}_translated_{slug}.pdf.json` to `translated_inplace/`
# MAGIC    — the SAME contract `server/pdf_render.py` renders, so the review UI is unchanged.
# MAGIC
# MAGIC Runs as the deploying user (job `run_as`); uses that identity's WorkspaceClient
# MAGIC for both the warehouse statement execution and the model REST calls.

# COMMAND ----------

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

# COMMAND ----------

# MAGIC %md
# MAGIC ## Parameters

# COMMAND ----------

dbutils.widgets.text("input_path", "", "Raw .pdf path (/Volumes/...)")
dbutils.widgets.text("output_dir", "", "translated_inplace dir")
dbutils.widgets.text("target_language", "English", "Target language")
dbutils.widgets.text("model_endpoint", "databricks-claude-sonnet-4-6", "FMAPI / AI Gateway endpoint")
dbutils.widgets.text("warehouse_id", "", "SQL warehouse id (for ai_parse_document)")
dbutils.widgets.text("glossary_delta_table", "", "Glossary Delta mirror FQN (empty = injection off)")
dbutils.widgets.text("custom_system_prompt", "", "System prompt frozen at upload (empty → built-in default)")
dbutils.widgets.text("max_workers", "8", "Concurrent translate workers")

input_path           = dbutils.widgets.get("input_path").strip()
output_dir           = dbutils.widgets.get("output_dir").strip().rstrip("/")
target_language      = dbutils.widgets.get("target_language").strip() or "English"
model_endpoint       = dbutils.widgets.get("model_endpoint").strip()
warehouse_id         = dbutils.widgets.get("warehouse_id").strip()
glossary_delta_table = dbutils.widgets.get("glossary_delta_table").strip()
base_prompt          = dbutils.widgets.get("custom_system_prompt")
MAX_WORKERS          = int(dbutils.widgets.get("max_workers").strip() or "8")

assert input_path and output_dir and warehouse_id, \
    "input_path, output_dir and warehouse_id are required"

MAX_TOKENS = 8192
MAX_GLOSSARY_INJECT = 20
PARSE_TIMEOUT_S = 300.0

_DEFAULT_PROMPT = (
    "You are an expert translator for clinical and regulated documents. Translate the "
    "user's text into {lang}. Preserve meaning, tone, numbers, units, and terminology "
    "exactly. Return ONLY the translation — no preamble, quotes, or markdown."
)
if not (base_prompt or "").strip():
    base_prompt = _DEFAULT_PROMPT


def _to_fs(p: str) -> str:
    return p[len("dbfs:"):] if p.startswith("dbfs:") else p


def _slug(lang: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (lang or "translated").lower()).strip("_") or "translated"


input_path = _to_fs(input_path)
stem = input_path.rsplit("/", 1)[-1]
if stem.lower().endswith(".pdf"):
    stem = stem[:-4]
artifact_path = f"{output_dir}/{stem}_translated_{_slug(target_language)}.pdf.json"

print(f"input_path    : {input_path}")
print(f"artifact_path : {artifact_path}")
print(f"target_language: {target_language}")
print(f"model_endpoint : {model_endpoint}")
print(f"warehouse_id   : {warehouse_id}")
print(f"glossary table : {glossary_delta_table or '(disabled)'}")

_w = WorkspaceClient()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 1 — parse (ai_parse_document on the warehouse)

# COMMAND ----------

def _sql_esc(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _sql_execute(statement: str, *, timeout_s: float = PARSE_TIMEOUT_S,
                 wait_timeout: str = "50s") -> dict:
    """Run one SQL statement on the warehouse; poll to terminal. Mirrors
    server/delta_sync._execute so the parse behaves exactly like the app's."""
    resp = _w.statement_execution.execute_statement(
        statement=statement, warehouse_id=warehouse_id, wait_timeout=wait_timeout)
    deadline = time.monotonic() + timeout_s
    while True:
        state = resp.status.state if resp.status else None
        if state == StatementState.SUCCEEDED:
            return resp.as_dict()
        if state in (StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED):
            err = (resp.status.error.message if (resp.status and resp.status.error) else str(state))
            raise RuntimeError(f"ai_parse SQL {state}: {err}")
        if time.monotonic() > deadline:
            try:
                _w.statement_execution.cancel_execution(resp.statement_id)
            except Exception:
                pass
            raise RuntimeError(f"ai_parse timed out after {timeout_s}s")
        time.sleep(0.5)
        resp = _w.statement_execution.get_statement(resp.statement_id)


def parse_pdf(pdf_path: str) -> list:
    stmt = (
        "SELECT to_json(ai_parse_document(content, map('version','2.0'))"
        ":document:elements) AS els "
        f"FROM read_files({_sql_esc(pdf_path)}, format => 'binaryFile')"
    )
    out = _sql_execute(stmt)
    data = (out.get("result") or {}).get("data_array") or []
    if not data or not data[0] or data[0][0] is None:
        raise RuntimeError("ai_parse_document returned no elements for " + pdf_path)
    return json.loads(data[0][0])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Source-language detection (script heuristic — no extra deps)

# COMMAND ----------

def detect_lang(texts: list) -> str:
    sample = " ".join((t or "") for t in texts[:30])[:3000]
    if not sample.strip():
        return "en"
    c = {"hira": 0, "kata": 0, "hangul": 0, "kanji": 0, "ar": 0, "he": 0, "cyr": 0, "thai": 0}
    for ch in sample:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F: c["hira"] += 1
        elif 0x30A0 <= cp <= 0x30FF: c["kata"] += 1
        elif 0xAC00 <= cp <= 0xD7AF: c["hangul"] += 1
        elif 0x4E00 <= cp <= 0x9FFF: c["kanji"] += 1
        elif 0x0600 <= cp <= 0x06FF: c["ar"] += 1
        elif 0x0590 <= cp <= 0x05FF: c["he"] += 1
        elif 0x0400 <= cp <= 0x04FF: c["cyr"] += 1
        elif 0x0E00 <= cp <= 0x0E7F: c["thai"] += 1
    if c["hira"] or c["kata"]: return "ja"
    if c["hangul"]: return "ko"
    if c["kanji"]: return "zh"
    if c["ar"]: return "ar"
    if c["he"]: return "he"
    if c["cyr"]: return "ru"
    if c["thai"]: return "th"
    return "en"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Approved-glossary pairs (from the Delta mirror)

# COMMAND ----------

# Same map the DOCX notebook uses — glossary target_lang is stored inconsistently
# (seed rows as a code 'en', mined/customer rows as the name 'english'), so we
# match BOTH forms below to keep DOCX and PDF injecting the same subset.
_LANG_NAME_TO_CODE = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
    "polish": "pl", "turkish": "tr", "arabic": "ar", "hindi": "hi",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
    "japanese": "ja", "korean": "ko", "chinese": "zh-cn",
    "simplified chinese": "zh-cn", "traditional chinese": "zh-tw",
}


def _lang_match_forms(lang: str) -> list:
    """Both forms a target language might be stored as: the name AND the code."""
    l = (lang or "").strip().lower()
    if not l:
        return []
    code = _LANG_NAME_TO_CODE.get(l, l[:2])
    return sorted({l, code})


def load_glossary(target_lang: str) -> list:
    """Approved (model_phrase, correction) pairs for this target language, read
    from the glossary Delta mirror. Matches both the language name and its code
    (see _LANG_NAME_TO_CODE). Best-effort — glossary injection is optional."""
    if not glossary_delta_table:
        return []
    forms = _lang_match_forms(target_lang)
    if not forms:
        return []
    in_list = ", ".join("'%s'" % f.replace("'", "''") for f in forms)
    try:
        rows = (
            spark.read.table(glossary_delta_table)
            .where("approved = true")
            .where(f"lower(target_lang) IN ({in_list})")
            .select("model_phrase", "correction")
            .collect()
        )
        pairs = [(r["model_phrase"], r["correction"]) for r in rows if (r["model_phrase"] or "").strip()]
        print(f"[glossary] {len(pairs)} approved terms for target={target_lang} (matched {forms})")
        return pairs
    except Exception as ex:
        print(f"[glossary] skipped ({ex})")
        return []

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 2 — translate (FMAPI / UC AI Gateway) — ported from server/pdf_translate.py

# COMMAND ----------

_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s+")
_TABLE_CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_TABLE_ADDENDUM = (
    "\n\nThe input is an HTML <table>. Translate ONLY the human-readable text "
    "inside cells to {lang}. Preserve every HTML tag, attribute, number, and the "
    "table structure EXACTLY. Use the surrounding cells for context. Return ONLY "
    "the resulting HTML table — no commentary, no code fences."
)
_cache: dict = {}


def _is_translatable(text: str) -> bool:
    return bool(text) and any(c.isalpha() for c in text)


def _clean_translation(out: str, source: str) -> str:
    if not out:
        return out
    cleaned = _MD_HEADER_RE.sub("", out).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'", "“", "「"):
        inner = cleaned[1:-1].strip()
        if inner:
            cleaned = inner
    return cleaned or source


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") in ("text", "output_text"))
    return ""


def _is_uc_gateway_endpoint(endpoint: str) -> bool:
    return (endpoint or "").count(".") >= 2


def _serving_chat(system: str, user: str) -> str:
    resp = _w.api_client.do(
        "POST", f"/serving-endpoints/{model_endpoint}/invocations",
        body={"messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}],
              "temperature": 0.0, "max_tokens": MAX_TOKENS})
    choices = (resp or {}).get("choices") or []
    if not choices:
        raise RuntimeError(f"serving endpoint returned no choices: {str(resp)[:200]}")
    return _content_to_text(choices[0].get("message", {}).get("content")).strip()


def _uc_gateway_responses(system: str, user: str) -> str:
    resp = _w.api_client.do(
        "POST", "/ai-gateway/mlflow/v1/responses",
        body={"model": model_endpoint, "instructions": system, "input": user,
              "max_output_tokens": MAX_TOKENS})
    txt = (resp or {}).get("output_text")
    if txt:
        return txt.strip()
    parts = [_content_to_text(it.get("content")) for it in ((resp or {}).get("output") or [])
             if it.get("type") == "message"]
    out = "".join(parts).strip()
    if not out:
        raise RuntimeError(f"UC gateway returned no text: {str(resp)[:200]}")
    return out


def _model_chat(system: str, user: str) -> str:
    if _is_uc_gateway_endpoint(model_endpoint):
        return _uc_gateway_responses(system, user)
    return _serving_chat(system, user)


def _system_prompt(target_lang: str, glossary_pairs: list, text: str) -> str:
    prompt = base_prompt.replace("{lang}", target_lang)
    matches = [(mp, corr) for mp, corr in glossary_pairs if mp and mp in text][:MAX_GLOSSARY_INJECT]
    if matches:
        lines = "\n".join(f'- "{mp}" -> "{corr}"' for mp, corr in matches)
        prompt += ("\n\nGLOSSARY — when the source contains the following terms, use the "
                   "specified translation verbatim:\n" + lines)
    return prompt


def _translate_text(text: str, target_lang: str, glossary_pairs: list) -> str:
    if not text or not text.strip():
        return text
    key = f"{target_lang}|{text}"
    if key in _cache:
        return _cache[key]
    try:
        out = _clean_translation(_model_chat(_system_prompt(target_lang, glossary_pairs, text), text), text) or text
    except Exception as e:
        print(f"  ! segment failed, keeping source: {e}")
        out = text
    _cache[key] = out
    return out


def _translate_table_cellwise(html: str, target_lang: str, glossary_pairs: list) -> str:
    def repl(m):
        open_t, inner, close_t = m.group(1), m.group(2), m.group(3)
        cell = _TAG_RE.sub("", inner).strip()
        if not cell:
            return m.group(0)
        return f"{open_t}{escape(_translate_text(cell, target_lang, glossary_pairs))}{close_t}"
    return _TABLE_CELL_RE.sub(repl, html or "")


def _translate_table(html: str, target_lang: str, glossary_pairs: list) -> str:
    if not html or "<t" not in html.lower():
        return html
    key = f"{target_lang}|TABLE|{html}"
    if key in _cache:
        return _cache[key]
    system = _system_prompt(target_lang, glossary_pairs, html) + _TABLE_ADDENDUM.replace("{lang}", target_lang)
    try:
        out = _FENCE_RE.sub("", _model_chat(system, html).strip()).strip()
        if "<table" not in out.lower():
            raise ValueError("model did not return an HTML table")
    except Exception as e:
        print(f"  ! whole-table translate failed, per-cell fallback: {e}")
        out = _translate_table_cellwise(html, target_lang, glossary_pairs)
    _cache[key] = out
    return out

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run: parse → translate → write artifact

# COMMAND ----------

def _el_source(el): return el.get("content") or ""
def _el_page(el):
    bbox = el.get("bbox") or [{}]
    return int((bbox[0].get("page_id") or 0)) + 1 if bbox else 1
def _el_coord(el):
    bbox = el.get("bbox") or []
    return bbox[0].get("coord") if bbox else None


raw_elements = parse_pdf(input_path)
print(f"parsed {len(raw_elements)} elements")

src_lang = detect_lang([_el_source(e) for e in raw_elements])
glossary_pairs = load_glossary(target_language)  # approved terms for this target language

def _translate_one(el: dict) -> dict:
    etype = el.get("type", "text")
    src = _el_source(el)
    if etype == "table":
        tgt = _translate_table(src, target_language, glossary_pairs)
    elif _is_translatable(src):
        tgt = _translate_text(src, target_language, glossary_pairs)
    else:
        tgt = src
    return {"id": int(el["id"]), "type": etype, "page": _el_page(el),
            "bbox": _el_coord(el), "source": src, "target": tgt}

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    elements = list(ex.map(_translate_one, raw_elements))
elements.sort(key=lambda e: e["id"])

artifact = {
    "source_lang": src_lang,
    "target_lang": target_language,
    "pages": max((e["page"] for e in elements), default=1),
    "model_endpoint": model_endpoint,
    "elements": elements,
}

# Write via the FUSE-mounted Volume path (whole-file write is reliable on
# serverless Volumes; matches how the DOCX notebook writes its output).
with open(artifact_path, "wb") as fh:
    fh.write(json.dumps(artifact, ensure_ascii=False).encode("utf-8"))
print(f"wrote {artifact_path} ({len(elements)} elements, source={src_lang})")

# The watcher parses this to record bronze_documents.source_language.
dbutils.notebook.exit(json.dumps({"source_language_code": src_lang,
                                  "artifact_path": artifact_path,
                                  "elements": len(elements)}))
