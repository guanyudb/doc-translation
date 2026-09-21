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
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
dbutils.widgets.dropdown("enable_batching", "true", ["true", "false"],
                        "Group small elements into one model request (cost)")
dbutils.widgets.text("batch_size", "8", "Max small elements per batched request")

input_path           = dbutils.widgets.get("input_path").strip()
output_dir           = dbutils.widgets.get("output_dir").strip().rstrip("/")
target_language      = dbutils.widgets.get("target_language").strip() or "English"
model_endpoint       = dbutils.widgets.get("model_endpoint").strip()
warehouse_id         = dbutils.widgets.get("warehouse_id").strip()
glossary_delta_table = dbutils.widgets.get("glossary_delta_table").strip()
base_prompt          = dbutils.widgets.get("custom_system_prompt")
MAX_WORKERS          = int(dbutils.widgets.get("max_workers").strip() or "8")
enable_batching      = dbutils.widgets.get("enable_batching").lower() == "true"
try:
    batch_size = max(1, int(dbutils.widgets.get("batch_size")))
except ValueError:
    batch_size = 8

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

# ---- Request batching (cost) ------------------------------------------------
# Amortize the (long) system prompt across several SMALL elements per request.
# Only genuinely small, non-table elements are eligible; large ones, anything
# with a literal <seg> tag, and cache hits take the single path. Strict <seg id>
# parsing with a per-segment fallback keeps correctness if a batch is malformed.
SEG_OPEN = '<seg id="'
SEG_CLOSE = "</seg>"
_SEG_COLLISION_RE = re.compile(r"</?seg\b", re.I)
_SEG_PARSE_RE = re.compile(r'<seg id="(\d+)">\s*(.*?)\s*</seg>', re.DOTALL)
BATCH_MAX_SEG_CHARS = 600      # an element longer than this is NEVER batched (single path)
BATCH_GLOSSARY_CAP = 40        # per-batch glossary UNION cap (single path stays 20)
BATCH_MAX_CHARS = 10_000       # total source chars per batch
BATCH_MAX_OUT_TOKENS = 7_000   # est. output tokens per batch (under the 8192 max)

BATCH_ADDENDUM = (
    "\n\nBATCH MODE — the user message contains {n} segments, each wrapped as "
    '<seg id="i">…</seg>. These rules OVERRIDE any output-format rule above:\n'
    "- Translate each segment INDEPENDENTLY into {lang}.\n"
    "- Return EXACTLY {n} segments using the SAME wrapper and the SAME ids, in the SAME "
    'order: <seg id="1">translation</seg><seg id="2">…</seg>.\n'
    "- Each segment contains ONLY that segment's translation — no commentary, quotes, "
    "labels, or code fences.\n"
    "- Never merge, split, reorder, drop, or renumber segments; never translate the <seg> "
    "tags or their ids.\n"
    "- Glossary rules apply per segment wherever a listed term appears.\n"
    "- An empty, whitespace, or punctuation/number-only segment is returned unchanged."
)

_stats_lock = threading.Lock()
_run_stats = {
    "batch_requests": 0, "single_requests": 0, "fallback_batches": 0,
    "segments": 0, "prompt_tokens": 0, "completion_tokens": 0, "oversized_solo": 0,
}


def _add_stats(**kw):
    with _stats_lock:
        for k, v in kw.items():
            _run_stats[k] = _run_stats.get(k, 0) + v


def _usage_dict(u) -> dict | None:
    """Normalize usage to {prompt_tokens, completion_tokens} across serving (prompt/
    completion) and the gateway Responses API (input/output)."""
    if u is None:
        return None
    def g(name):
        return u.get(name) if isinstance(u, dict) else getattr(u, name, None)
    p = g("prompt_tokens")
    p = p if p is not None else g("input_tokens")
    c = g("completion_tokens")
    c = c if c is not None else g("output_tokens")
    if p is None and c is None:
        return None
    return {"prompt_tokens": p, "completion_tokens": c}


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


def _serving_invoke(body: dict) -> dict:
    """POST to the serving endpoint, retrying once without the offending param for reasoning
    models (e.g. GPT-5) that reject temperature != default or want max_completion_tokens."""
    path = f"/serving-endpoints/{model_endpoint}/invocations"
    try:
        return _w.api_client.do("POST", path, body=body)
    except Exception as ex:
        msg = str(ex).lower()
        retried = dict(body)
        changed = False
        if "temperature" in msg and "temperature" in retried:
            retried.pop("temperature"); changed = True
        if ("max_completion_tokens" in msg or "max_tokens" in msg) and "max_tokens" in retried:
            retried["max_completion_tokens"] = retried.pop("max_tokens"); changed = True
        if not changed:
            raise
        print(f"  [model] endpoint rejected a param ({msg[:120]}); retrying without it")
        return _w.api_client.do("POST", path, body=retried)


def _serving_chat(system: str, user: str) -> tuple[str, dict | None]:
    resp = _serving_invoke({"messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
                            "temperature": 0.0, "max_tokens": MAX_TOKENS})
    choices = (resp or {}).get("choices") or []
    if not choices:
        raise RuntimeError(f"serving endpoint returned no choices: {str(resp)[:200]}")
    return _content_to_text(choices[0].get("message", {}).get("content")).strip(), \
        _usage_dict((resp or {}).get("usage"))


def _uc_gateway_responses(system: str, user: str) -> tuple[str, dict | None]:
    resp = _w.api_client.do(
        "POST", "/ai-gateway/mlflow/v1/responses",
        body={"model": model_endpoint, "instructions": system, "input": user,
              "max_output_tokens": MAX_TOKENS})
    usage = _usage_dict((resp or {}).get("usage"))
    txt = (resp or {}).get("output_text")
    if txt:
        return txt.strip(), usage
    parts = [_content_to_text(it.get("content")) for it in ((resp or {}).get("output") or [])
             if it.get("type") == "message"]
    out = "".join(parts).strip()
    if not out:
        raise RuntimeError(f"UC gateway returned no text: {str(resp)[:200]}")
    return out, usage


def _model_chat(system: str, user: str) -> tuple[str, dict | None]:
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
        raw, usage = _model_chat(_system_prompt(target_lang, glossary_pairs, text), text)
        out = _clean_translation(raw, text) or text
        _add_stats(single_requests=1, segments=1,
                   prompt_tokens=(usage or {}).get("prompt_tokens") or 0,
                   completion_tokens=(usage or {}).get("completion_tokens") or 0)
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
        raw, usage = _model_chat(system, html)
        out = _FENCE_RE.sub("", (raw or "").strip()).strip()
        if "<table" not in out.lower():
            raise ValueError("model did not return an HTML table")
        _add_stats(single_requests=1, segments=1,
                   prompt_tokens=(usage or {}).get("prompt_tokens") or 0,
                   completion_tokens=(usage or {}).get("completion_tokens") or 0)
    except Exception as e:
        print(f"  ! whole-table translate failed, per-cell fallback: {e}")
        out = _translate_table_cellwise(html, target_lang, glossary_pairs)
    _cache[key] = out
    return out


def _est_out_tokens(chars: int, n: int) -> int:
    return math.ceil(0.75 * chars) + 40 * n


def _translate_batch(texts: list, target_lang: str, glossary_pairs: list) -> list:
    """Translate several small text elements in ONE request via <seg id="k">…</seg>. STRICT
    parse (ids must be exactly 1..n) or per-segment fallback via _translate_text. Cached under
    the same f"{target_lang}|{text}" key _translate_text uses. Tables never come here."""
    if len(texts) == 1:
        return [_translate_text(texts[0], target_lang, glossary_pairs)]

    # Glossary UNION across the batch (substring match), longest-wins, capped.
    union = {}
    for t in texts:
        for mp, corr in glossary_pairs:
            if mp and mp in t:
                union.setdefault(mp, corr)
    ordered = sorted(union.items(), key=lambda kv: -len(kv[0]))
    kept = []
    for mp, corr in ordered:
        if any(mp in longer for longer, _ in kept):
            continue
        kept.append((mp, corr))
    kept = kept[:BATCH_GLOSSARY_CAP]

    system = base_prompt.replace("{lang}", target_lang)
    if kept:
        lines = "\n".join(f'- "{mp}" -> "{corr}"' for mp, corr in kept)
        system += ("\n\nGLOSSARY — when the source contains the following terms, use the "
                   "specified translation verbatim:\n" + lines)
    system += BATCH_ADDENDUM.format(n=len(texts), lang=target_lang)
    user = "".join(f'{SEG_OPEN}{i}">{t}{SEG_CLOSE}\n' for i, t in enumerate(texts, start=1))

    def _fallback(reason):
        _add_stats(fallback_batches=1)
        print(f"  [batch] {reason} → per-segment fallback ({len(texts)} segments)")
        return [_translate_text(t, target_lang, glossary_pairs) for t in texts]

    try:
        raw, usage = _model_chat(system, user)
    except Exception as e:
        return _fallback(f"model error: {e}")

    raw = _FENCE_RE.sub("", (raw or "").strip()).strip()
    found = {int(m.group(1)): m.group(2) for m in _SEG_PARSE_RE.finditer(raw)}
    if sorted(found.keys()) != list(range(1, len(texts) + 1)):
        return _fallback(f"segment id mismatch (got {sorted(found.keys())[:12]})")

    outs = []
    for i, t in enumerate(texts, start=1):
        out = _clean_translation(found[i], t) or t
        _cache[f"{target_lang}|{t}"] = out
        outs.append(out)
    _add_stats(batch_requests=1, segments=len(texts),
               prompt_tokens=(usage or {}).get("prompt_tokens") or 0,
               completion_tokens=(usage or {}).get("completion_tokens") or 0)
    return outs


def _pack_batches(texts: list) -> list:
    """Greedily bin-pack eligible small elements under the count / char / output-token caps."""
    batches, cur, cur_chars = [], [], 0
    for t in texts:
        c = len(t)
        if cur and (len(cur) >= batch_size or cur_chars + c > BATCH_MAX_CHARS
                    or _est_out_tokens(cur_chars + c, len(cur) + 1) > BATCH_MAX_OUT_TOKENS):
            batches.append(cur); cur, cur_chars = [], 0
        cur.append(t); cur_chars += c
    if cur:
        batches.append(cur)
    return batches


def translate_all(texts: list, ex: ThreadPoolExecutor, target_lang: str, glossary_pairs: list) -> list:
    """Translate element sources preserving order; batch eligible small, uncached ones,
    everything else (empty, cache hit, <seg> collision, oversized) single. All work goes to
    the passed-in executor. enable_batching=false → all singles (today's behavior)."""
    n = len(texts)
    result = [""] * n
    positions, order = {}, []
    for i, t in enumerate(texts):
        if t not in positions:
            positions[t] = []; order.append(t)
        positions[t].append(i)

    if not enable_batching:
        futs = {ex.submit(_translate_text, t, target_lang, glossary_pairs): t for t in order}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # _translate_text shouldn't raise; keep the phase crash-proof
                print(f"  ! translate future failed, keeping source: {e}")
                r = t
            for i in positions[t]:
                result[i] = r
        return result

    batchable, singles = [], []
    for t in order:
        if not t or not t.strip():
            singles.append(t); continue
        if f"{target_lang}|{t}" in _cache:
            singles.append(t); continue
        if _SEG_COLLISION_RE.search(t):
            singles.append(t); continue
        if len(t) > BATCH_MAX_SEG_CHARS:
            _add_stats(oversized_solo=1); singles.append(t); continue
        batchable.append(t)

    trans, futs = {}, {}
    for t in singles:
        futs[ex.submit(_translate_text, t, target_lang, glossary_pairs)] = ("single", t)
    for group in _pack_batches(batchable):
        futs[ex.submit(_translate_batch, group, target_lang, glossary_pairs)] = ("batch", group)
    for fut in as_completed(futs):
        kind, payload = futs[fut]
        try:
            res = fut.result()
        except Exception as e:  # _translate_batch/_translate_text shouldn't raise; be defensive
            print(f"  ! {kind} translate future failed, keeping source: {e}")
            res = payload if kind == "single" else list(payload)  # identity fallback
        if kind == "single":
            trans[payload] = res
        else:
            for gt, go in zip(payload, res):
                trans[gt] = go
    for t in order:
        r = trans.get(t, t)
        for i in positions[t]:
            result[i] = r
    return result

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

# Partition elements: tables stay unbatched (whole-table + cellwise fallback), plain text
# elements are batched (translate_all), and non-translatable elements keep their source.
table_els = [e for e in raw_elements if e.get("type") == "table"]
text_els  = [e for e in raw_elements if e.get("type") != "table" and _is_translatable(_el_source(e))]

tgt_by_id: dict[int, str] = {
    int(e["id"]): _el_source(e)
    for e in raw_elements
    if e.get("type") != "table" and not _is_translatable(_el_source(e))
}
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    tfuts = {ex.submit(_translate_table, _el_source(e), target_language, glossary_pairs): int(e["id"])
             for e in table_els}
    text_outs = translate_all([_el_source(e) for e in text_els], ex, target_language, glossary_pairs)
    for e, out in zip(text_els, text_outs):
        tgt_by_id[int(e["id"])] = out
    for fut in as_completed(tfuts):
        tgt_by_id[tfuts[fut]] = fut.result()

elements = [{"id": int(e["id"]), "type": e.get("type", "text"), "page": _el_page(e),
             "bbox": _el_coord(e), "source": _el_source(e), "target": tgt_by_id[int(e["id"])]}
            for e in raw_elements]
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

print("=== Batching stats ===")
print(f"  batching: {'on' if enable_batching else 'off'} (batch_size={batch_size})")
print(f"  requests: {_run_stats['batch_requests']} batched + {_run_stats['single_requests']} single"
      f"  |  segments: {_run_stats['segments']}"
      f"  |  fallback batches: {_run_stats['fallback_batches']}"
      f"  |  oversized→solo: {_run_stats['oversized_solo']}")
print(f"  tokens: {_run_stats['prompt_tokens']} prompt + {_run_stats['completion_tokens']} completion")

# The watcher parses this to record bronze_documents.source_language.
dbutils.notebook.exit(json.dumps({"source_language_code": src_lang,
                                  "artifact_path": artifact_path,
                                  "elements": len(elements),
                                  "enable_batching": enable_batching,
                                  "batch_size": batch_size,
                                  **_run_stats}))
