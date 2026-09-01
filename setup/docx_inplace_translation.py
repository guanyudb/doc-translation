# Databricks notebook source
# MAGIC %md
# MAGIC # In-Place DOCX Translation (Gemini via FMAPI)
# MAGIC
# MAGIC Translates `.docx` files **in place at the XML level** so the output preserves the
# MAGIC exact original layout — fonts, styles, page setup, headers/footers, footnotes,
# MAGIC tables, native charts, numbering, TOC fields, hyperlinks, page breaks.
# MAGIC We never reconstruct the document; we only swap the text inside `<w:t>` nodes.
# MAGIC
# MAGIC **Inputs (widgets):**
# MAGIC - `input_path` — a single `.docx` file *or* a folder containing `.docx` files (Volumes path supported)
# MAGIC - `output_dir` — Volume folder where translated files are written
# MAGIC - `target_language` — e.g., `English`, `Spanish`, `French`
# MAGIC - `model_endpoint` — FMAPI serving endpoint (default `databricks-gemini-3-1-pro`)
# MAGIC - `max_workers` — parallel translation requests per file
# MAGIC - `max_pages` — limit body translation to the first N pages (`0` = full document).
# MAGIC   Detects page boundaries from `<w:lastRenderedPageBreak/>` (auto-written by Word
# MAGIC   on save) and `<w:br w:type="page"/>` hard breaks. Applies only to the main body;
# MAGIC   headers, footers, footnotes, and charts are always translated in full.
# MAGIC
# MAGIC **What gets translated:**
# MAGIC - `word/document.xml` — body text, tables, **textbox/shape text** (nested `<w:p>` inside `<w:txbxContent>`)
# MAGIC - `word/header*.xml`, `word/footer*.xml` — running headers/footers
# MAGIC - `word/footnotes.xml`, `word/endnotes.xml`, `word/comments.xml`
# MAGIC - `word/charts/chart*.xml` — native chart titles, axis labels, series names, text data labels
# MAGIC - `word/diagrams/*.xml` — **SmartArt** node text (process flows, hierarchies, etc.)
# MAGIC
# MAGIC **What stays untouched (by design):**
# MAGIC - All formatting, styles, themes
# MAGIC - Field codes, bookmarks, cross-references, TOC entries (Word will refresh on open)
# MAGIC - **Raster images** (PNG/JPEG/etc. embedded in `word/media/`) — text baked into pixels
# MAGIC   needs OCR + redraw, which is a separate pipeline.
# MAGIC - OLE objects, EMF/WMF vector images
# MAGIC - Numeric chart values (we skip pure-number text nodes)
# MAGIC
# MAGIC **Output:** for each input `foo.docx`, writes `foo_translated_<lang>.docx` to `output_dir`,
# MAGIC and a side-by-side comparison DataFrame for review.

# COMMAND ----------

# MAGIC %pip install lxml langdetect pyahocorasick --quiet
# MAGIC # Some workspace serverless notebook environments ship an older
# MAGIC # databricks-sdk that lacks `serving_endpoints.get_open_ai_client()`
# MAGIC # (deploy guide gotcha #14). Pin a known-good version.
# MAGIC %pip install --quiet --upgrade "databricks-sdk>=0.30.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("input_path", "/Volumes/clindevasia_us_east_1_dev/translation_poc/raw_documents/", "Input DOCX file or folder")
dbutils.widgets.text("output_dir", "/Volumes/clindevasia_us_east_1_dev/translation_poc/translated_inplace", "Output folder")
dbutils.widgets.text("target_language", "English", "Target language (full name, e.g. English)")
dbutils.widgets.text("model_endpoint", "databricks-gemini-3-1-pro", "FMAPI model endpoint")
dbutils.widgets.text("max_workers", "8", "Concurrent translation workers per file")
dbutils.widgets.text("max_pages", "0", "Limit body to first N pages (0 = no limit)")
dbutils.widgets.dropdown("skip_if_already_target", "true", ["true", "false"],
                        "Skip paragraphs already in target language")
dbutils.widgets.text("glossary_delta_table", "",
                     "FQN of translation_glossary Delta mirror (empty = disabled)")
dbutils.widgets.text("custom_system_prompt", "",
                     "Custom system prompt (empty = built-in default)")

input_path = dbutils.widgets.get("input_path").strip()
output_dir = dbutils.widgets.get("output_dir").strip().rstrip("/")
target_language = dbutils.widgets.get("target_language").strip()
model_endpoint = dbutils.widgets.get("model_endpoint").strip()
max_workers = int(dbutils.widgets.get("max_workers"))
max_pages = int(dbutils.widgets.get("max_pages"))
skip_if_already_target = dbutils.widgets.get("skip_if_already_target").lower() == "true"
glossary_delta_table = dbutils.widgets.get("glossary_delta_table").strip()
# Not .strip() — a custom prompt's leading/trailing whitespace could be
# intentional; only treat it as "unset" when it's blank.
custom_system_prompt = dbutils.widgets.get("custom_system_prompt")

print(f"Input:     {input_path}")
print(f"Output:    {output_dir}")
print(f"Language:  {target_language}")
print(f"Model:     {model_endpoint}")
print(f"Workers:   {max_workers}")
print(f"Max pages: {'no limit' if max_pages <= 0 else max_pages}")
print(f"Skip already-target: {skip_if_already_target}")
print(f"Glossary table: {glossary_delta_table or '(disabled)'}")

# COMMAND ----------

import os
import re
import zipfile
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from lxml import etree
from databricks.sdk import WorkspaceClient

# OOXML namespaces
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSMAP = {"w": W_NS}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Files inside the .docx zip we should walk for paragraph text
WORD_TEXT_PREFIXES = (
    "word/header",
    "word/footer",
    "word/footnotes",
    "word/endnotes",
    "word/comments",
)
CHART_PREFIX = "word/charts/chart"
DIAGRAM_PREFIX = "word/diagrams/"

# Detect "real" text (any letter from any major script). Pure numbers / symbols are skipped.
LETTER_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u0370-\u1CFF\u1E00-\uFFFF]")


# COMMAND ----------

# MAGIC %md
# MAGIC ## File discovery

# COMMAND ----------

def list_docx_files(path: str) -> list[str]:
    """Return sorted list of .docx files. Accepts a file path or a folder."""
    if path.endswith(".docx") and os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        return sorted(
            str(p) for p in Path(path).rglob("*.docx")
            if not p.name.startswith("~$")  # skip Word lock files
        )
    raise FileNotFoundError(f"Not a .docx file or folder: {path}")


files = list_docx_files(input_path)
print(f"Found {len(files)} DOCX file(s):")
for f in files:
    print(f"  {f}")


def inspect_docx_assets(path: str) -> dict:
    """Quick inventory of image-like assets so you know what will/won't be translated."""
    counts = {
        "raster_images": 0,    # PNG/JPG/GIF in word/media/  → pixel text NOT translatable
        "wmf_emf_vectors": 0,  # EMF/WMF in word/media/      → vector text NOT translatable
        "charts": 0,           # word/charts/chart*.xml      → translated
        "smartart_diagrams": 0,# word/diagrams/data*.xml     → translated
        "ole_objects": 0,      # embeddings/oleObject*       → NOT translatable
    }
    raster_exts = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff")
    with zipfile.ZipFile(path, "r") as z:
        for name in z.namelist():
            low = name.lower()
            if low.startswith("word/media/"):
                if low.endswith(raster_exts):
                    counts["raster_images"] += 1
                elif low.endswith((".wmf", ".emf")):
                    counts["wmf_emf_vectors"] += 1
            elif low.startswith("word/charts/chart") and low.endswith(".xml"):
                counts["charts"] += 1
            elif low.startswith("word/diagrams/data") and low.endswith(".xml"):
                counts["smartart_diagrams"] += 1
            elif "embeddings/oleobject" in low:
                counts["ole_objects"] += 1
    return counts


inventory_rows = [{"file": os.path.basename(f), **inspect_docx_assets(f)} for f in files]
print("\nAsset inventory (per file):")
display(pd.DataFrame(inventory_rows))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Language detection (skip already-target paragraphs)
# MAGIC
# MAGIC Before sending text to Gemini, we detect the source language locally with `langdetect`.
# MAGIC If a paragraph is confidently already in the target language (e.g. an English table in
# MAGIC an English-target run), we skip the API call entirely. Saves money and avoids the LLM
# MAGIC subtly re-paraphrasing already-correct text.
# MAGIC
# MAGIC Detection is skipped (text always sent to LLM) for very short strings (< `LANGDETECT_MIN_CHARS`)
# MAGIC because language detection is unreliable on short text.

# COMMAND ----------

from langdetect import detect_langs, DetectorFactory, LangDetectException
DetectorFactory.seed = 0  # make detection deterministic

# Map common full-name target languages to ISO 639-1 codes that langdetect emits
LANG_NAME_TO_CODE = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
    "polish": "pl", "turkish": "tr", "arabic": "ar", "hindi": "hi",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
    "japanese": "ja", "korean": "ko", "chinese": "zh-cn",
    "simplified chinese": "zh-cn", "traditional chinese": "zh-tw",
}

target_lang_code = LANG_NAME_TO_CODE.get(target_language.lower(), target_language.lower()[:2])

LANGDETECT_MIN_CHARS = 30
LANGDETECT_MIN_CONF = 0.85

_skipped_already_target = 0

# Script families. We never skip a paragraph that contains characters from a
# script family OUTSIDE the target's family — bilingual cells common in clinical
# trial / regulatory documents would otherwise sneak past langdetect.
SCRIPT_PATTERNS = {
    "cjk":        re.compile(r"[\u3000-\u9FFF\uF900-\uFAFF\uFF66-\uFF9F]"),  # Hiragana/Katakana/Han
    "hangul":     re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]"),
    "cyrillic":   re.compile(r"[\u0400-\u04FF\u0500-\u052F]"),
    "arabic":     re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]"),
    "devanagari": re.compile(r"[\u0900-\u097F]"),
    "thai":       re.compile(r"[\u0E00-\u0E7F]"),
    "hebrew":     re.compile(r"[\u0590-\u05FF]"),
    "greek":      re.compile(r"[\u0370-\u03FF]"),
}

LANG_TO_SCRIPT_FAMILY = {
    "en": "latin", "es": "latin", "fr": "latin", "de": "latin", "it": "latin",
    "pt": "latin", "nl": "latin", "pl": "latin", "tr": "latin", "vi": "latin",
    "id": "latin", "ms": "latin",
    "ru": "cyrillic", "ar": "arabic", "hi": "devanagari", "th": "thai",
    "ja": "cjk", "ko": "hangul",
    "zh-cn": "cjk", "zh-tw": "cjk",
}

target_script_family = LANG_TO_SCRIPT_FAMILY.get(target_lang_code, "latin")


def has_foreign_script(text: str) -> bool:
    """Return True if text contains characters from a script outside the target family."""
    for fam, pat in SCRIPT_PATTERNS.items():
        if fam == target_script_family:
            continue
        if pat.search(text):
            return True
    # If target is a non-Latin family, treat presence of Latin letters as "foreign"
    # (a Cyrillic-target doc with English chunks should still be translated).
    if target_script_family != "latin" and re.search(r"[A-Za-z]{4,}", text):
        return True
    return False


def is_already_target_language(text: str) -> bool:
    """Return True if `text` is confidently already in the target language."""
    stripped = text.strip()
    if len(stripped) < LANGDETECT_MIN_CHARS:
        return False  # too short to detect reliably; let the LLM handle it
    if has_foreign_script(stripped):
        return False  # bilingual / mixed-script text — never skip
    try:
        ranked = detect_langs(stripped)
        if not ranked:
            return False
        top = ranked[0]
        # Match either exact code ("en") or family prefix ("zh-cn" vs "zh")
        return (
            (top.lang == target_lang_code or top.lang.startswith(target_lang_code.split("-")[0]))
            and top.prob >= LANGDETECT_MIN_CONF
        )
    except LangDetectException:
        return False


print(f"Target language code: {target_lang_code!r}  |  script family: {target_script_family!r}")

# Reverse map: ISO code → full English name, for reporting the detected source.
CODE_TO_LANG_NAME = {v: k.title() for k, v in LANG_NAME_TO_CODE.items()}


def detect_document_language(sample_texts: list[str]) -> tuple[str, str]:
    """Detect the dominant SOURCE language of a document from a sample of its
    paragraphs. Returns (iso_code, full_name). Falls back to ('und', 'Unknown')
    when detection is inconclusive.

    We concatenate the longest non-target paragraphs (up to a budget) and run
    langdetect once — document-level detection is far more stable than
    per-paragraph, and one detection is enough to tag the document + pick the
    glossary source language."""
    budget, acc = 4000, []
    for t in sorted((s.strip() for s in sample_texts if s and s.strip()),
                    key=len, reverse=True):
        acc.append(t)
        if sum(len(x) for x in acc) >= budget:
            break
    blob = "\n".join(acc).strip()
    if len(blob) < LANGDETECT_MIN_CHARS:
        return "und", "Unknown"
    try:
        ranked = detect_langs(blob)
        if not ranked:
            return "und", "Unknown"
        code = ranked[0].lang
        return code, CODE_TO_LANG_NAME.get(code, code)
    except LangDetectException:
        return "und", "Unknown"


# COMMAND ----------

# MAGIC %md
# MAGIC ## LLM translation client (FMAPI / Gemini)
# MAGIC
# MAGIC Uses the workspace's OpenAI-compatible serving client. Identical paragraphs are cached
# MAGIC so repeated boilerplate (e.g. headers, footnote labels) only costs one API call.

# COMMAND ----------

_w = WorkspaceClient()
_oai = _w.serving_endpoints.get_open_ai_client()

# XML 1.0 character validity. Strip anything not in the legal ranges so we
# never crash lxml on a stray control byte from the LLM. Allowed: \t \n \r,
# \x20-\uD7FF, \uE000-\uFFFD. Disallowed: C0 controls (except \t\n\r), DEL,
# C1 controls, surrogates, non-characters.
_XML_INVALID_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F"           # C0 controls (kept: \t \n \r)
    r"\x7F-\x84\x86-\x9F"                      # DEL + C1 controls
    r"\uD800-\uDFFF"                           # surrogates
    r"\uFDD0-\uFDEF"                           # non-characters
    r"\uFFFE\uFFFF]"                           # non-characters
)


def sanitize_for_xml(s: str) -> str:
    """Remove XML-1.0-illegal characters so lxml won't reject the string."""
    if not s:
        return s
    return _XML_INVALID_RE.sub("", s)


# Diagnostics: track which side (source DOCX vs LLM output) is producing
# XML-illegal control characters. Source-side counts indicate the original file
# already had bad bytes (possibly pasted from PDFs/OCR); LLM-side counts indicate
# the model is the one introducing them.
_xml_strip_stats = {
    "source_paragraphs_with_bad_chars": 0,
    "source_chars_stripped": 0,
    "llm_outputs_with_bad_chars": 0,
    "llm_chars_stripped": 0,
    "samples_source": [],   # up to 10 (codepoint, snippet) tuples
    "samples_llm": [],
}


def _audit_bad_chars(text: str) -> tuple[int, list[int]]:
    """Return (count, sorted unique codepoints) of XML-illegal chars in text."""
    bad = _XML_INVALID_RE.findall(text)
    cps = sorted({ord(c) for c in bad})
    return len(bad), cps


def _record_source_bad(text: str) -> None:
    n, cps = _audit_bad_chars(text)
    if n == 0:
        return
    _xml_strip_stats["source_paragraphs_with_bad_chars"] += 1
    _xml_strip_stats["source_chars_stripped"] += n
    if len(_xml_strip_stats["samples_source"]) < 10:
        _xml_strip_stats["samples_source"].append({
            "codepoints": [hex(c) for c in cps],
            "snippet": (text[:80] + "…") if len(text) > 80 else text,
        })


def _record_llm_bad(text: str) -> None:
    n, cps = _audit_bad_chars(text)
    if n == 0:
        return
    _xml_strip_stats["llm_outputs_with_bad_chars"] += 1
    _xml_strip_stats["llm_chars_stripped"] += n
    if len(_xml_strip_stats["samples_llm"]) < 10:
        _xml_strip_stats["samples_llm"].append({
            "codepoints": [hex(c) for c in cps],
            "snippet": (text[:80] + "…") if len(text) > 80 else text,
        })

# Built-in fallback prompt, used when no per-document custom prompt is supplied
# (e.g. a file dropped straight into the Volume rather than uploaded via the app).
# Mirrors server/prompts.py:DEFAULT_PROMPT_BODY and the postdeploy seed — keep the
# three in sync. `{lang}` is substituted per-call in _system_prompt_for().
TRANSLATE_SYSTEM = (
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

# A per-document prompt (chosen by the reviewer at upload, frozen into a sidecar,
# passed through by the watcher) fully replaces the built-in prompt. Glossary
# injection still appends afterward in _system_prompt_for().
if custom_system_prompt.strip():
    TRANSLATE_SYSTEM = custom_system_prompt
    print("  [prompt] using custom per-document system prompt "
          f"({len(custom_system_prompt)} chars)")
else:
    print("  [prompt] using built-in default system prompt")

# ---- Glossary prompt injection (Aho-Corasick, retrieval-based) --------------
#
# We read the translation_glossary Delta mirror once at startup and build a
# single Aho-Corasick automaton keyed on `model_phrase` (the source-language
# term). For each paragraph segment we scan the SOURCE text and inject only the
# glossary entries whose term actually appears — so the per-call prompt cost is
# a function of what's in *this* segment, not the size of the whole glossary.
# This is why the design scales to large enterprise glossaries: matching is
# O(segment length + matches), independent of total entry count.

MAX_GLOSSARY_INJECT = 20   # cap matched entries injected into any one segment

_glossary_automaton = None
_glossary_entry_count = 0


def _build_glossary_automaton():
    """Load approved glossary entries for this target language from the Delta
    mirror and build an Aho-Corasick automaton over their source-language
    phrases. Returns (automaton_or_None, entry_count). Never raises — glossary
    is a quality boost, never a hard dependency of translation."""
    if not glossary_delta_table:
        return None, 0
    try:
        import ahocorasick
    except ImportError:
        print("  [glossary] pyahocorasick not installed — skipping injection")
        return None, 0
    try:
        rows = (
            spark.read.table(glossary_delta_table)
            .where("approved = true AND target_lang = '%s'" % target_lang_code)
            .select("model_phrase", "correction")
            .collect()
        )
    except Exception as ex:
        print(f"  [glossary] could not read {glossary_delta_table}: {ex}")
        return None, 0

    A = ahocorasick.Automaton()
    n = 0
    for r in rows:
        phrase = (r["model_phrase"] or "").strip()
        corr = (r["correction"] or "").strip()
        # Only phrases with real content; the automaton matches substrings, so
        # 1-char keys would fire far too often. Require >= 2 chars.
        if len(phrase) >= 2 and corr:
            A.add_word(phrase, (phrase, corr))
            n += 1
    if n == 0:
        return None, 0
    A.make_automaton()
    return A, n


def glossary_matches(text: str) -> list[tuple[str, str]]:
    """Return (source_phrase, required_target_term) pairs whose source phrase
    occurs in `text`. Longest matches first, deduped, capped. Empty if the
    automaton is disabled or nothing matches.

    A shorter phrase that is a substring of a longer matched phrase is dropped
    (e.g. drop "医師"/physician when "治験責任医師"/principal-investigator also
    matched) so the injected rules don't fight each other. The default LLM
    translation still handles the shorter term correctly where it stands alone."""
    if _glossary_automaton is None or not text:
        return []
    hits: dict[str, str] = {}
    for _end, (phrase, corr) in _glossary_automaton.iter(text):
        hits[phrase] = corr
    if not hits:
        return []
    ordered = sorted(hits.items(), key=lambda kv: -len(kv[0]))
    kept: list[tuple[str, str]] = []
    for phrase, corr in ordered:
        if any(phrase in longer for longer, _ in kept):
            continue
        kept.append((phrase, corr))
    return kept[:MAX_GLOSSARY_INJECT]


def _system_prompt_for(text: str) -> str:
    """Base system prompt + any glossary rules relevant to this segment."""
    # str.replace (not str.format): a custom prompt may contain literal '{'/'}'
    # (e.g. JSON examples) that would raise KeyError/ValueError under .format.
    base = TRANSLATE_SYSTEM.replace("{lang}", target_language)
    matches = glossary_matches(text)
    if not matches:
        return base
    lines = "\n".join(f'- "{src}" → "{tgt}"' for src, tgt in matches)
    return (
        base
        + "\n\nGLOSSARY — when the source contains the following terms, use the "
          "specified translation verbatim (these are approved, required "
          "terminology; they override your default word choice):\n"
        + lines
    )


# Build the automaton once, at import time. Safe to call with the feature off
# (returns None) — llm_translate then behaves exactly as before.
_glossary_automaton, _glossary_entry_count = _build_glossary_automaton()
if _glossary_entry_count:
    print(f"  [glossary] injection enabled — {_glossary_entry_count} entries "
          f"loaded for target '{target_lang_code}'")
else:
    print("  [glossary] injection inactive (no table, no matches, or lib missing)")


_translation_cache: dict[str, str] = {}


def llm_translate(text: str) -> str:
    """Translate a single string. Cached by (lang, glossary, text).
    Short-circuits when `skip_if_already_target` is on and the text is
    confidently already in the target language."""
    global _skipped_already_target
    if not text or not text.strip():
        return text

    # Audit: did the SOURCE text already contain XML-illegal characters?
    _record_source_bad(text)
    # Sanitize input before sending to the model — keeps us from being blamed
    # for "LLM emitting bad chars" when the bad chars came from the source file.
    text_for_llm = sanitize_for_xml(text)

    # Glossary matches are deterministic for a given text, so folding them into
    # the cache key keeps caching correct while letting identical boilerplate
    # (which matches the same terms) still share one API call.
    matches = glossary_matches(text_for_llm)
    gloss_key = "|".join(f"{s}>{t}" for s, t in matches)
    cache_key = hashlib.md5(
        f"{target_language}|{gloss_key}|{text_for_llm}".encode("utf-8")
    ).hexdigest()
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    if skip_if_already_target and is_already_target_language(text_for_llm):
        _skipped_already_target += 1
        _translation_cache[cache_key] = text_for_llm
        return text_for_llm

    try:
        resp = _oai.chat.completions.create(
            model=model_endpoint,
            messages=[
                {"role": "system", "content": _system_prompt_for(text_for_llm)},
                {"role": "user", "content": text_for_llm},
            ],
            temperature=0.0,
            max_tokens=8192,
        )
        raw_out = (resp.choices[0].message.content or text_for_llm).strip()
        # Audit: did the LLM emit any XML-illegal characters?
        _record_llm_bad(raw_out)
        out = sanitize_for_xml(raw_out)
        # Preserve original leading/trailing whitespace pattern (using the
        # sanitized source text so we don't reintroduce illegal chars).
        if text_for_llm != text_for_llm.strip():
            leading = text_for_llm[: len(text_for_llm) - len(text_for_llm.lstrip())]
            trailing = text_for_llm[len(text_for_llm.rstrip()) :]
            out = f"{leading}{out}{trailing}"
        _translation_cache[cache_key] = out
        return out
    except Exception as ex:
        print(f"[warn] translation failed ({len(text)} chars): {ex}")
        return text_for_llm


# COMMAND ----------

# MAGIC %md
# MAGIC ## XML helpers — segment-aware in-place edit
# MAGIC
# MAGIC For each `<w:p>` we walk its descendants in document order and split into
# MAGIC **translatable segments** at boundaries that should preserve layout:
# MAGIC
# MAGIC - `<w:fldChar>` begin/end (skips field-generated text like TOC page numbers, cross-refs)
# MAGIC - `<w:fldSimple>` (older field format)
# MAGIC - `<w:tab/>` and `<w:br/>` (preserves tab stops with dot leaders, soft line breaks)
# MAGIC - Nested `<w:p>` boundaries (textbox content is processed by its own pass)
# MAGIC
# MAGIC Each segment is translated independently and written back into the segment's first
# MAGIC `<w:t>`, so TOC entries keep their page numbers, cross-references stay valid, and tab
# MAGIC stops still pull text to the right margin.

# COMMAND ----------

def is_translatable(text: str) -> bool:
    """True only if text has at least one alphabetic letter."""
    if not text or not text.strip():
        return False
    return bool(LETTER_RE.search(text))


def count_page_breaks_in_paragraph(p_elem) -> int:
    """Count page boundaries inside a paragraph.

    Two signals:
      - `<w:lastRenderedPageBreak/>`: written by Word/LibreOffice on save, marks
        exactly where the document was paginated last time it was rendered. Most
        reliable for "actual page" semantics.
      - `<w:br w:type="page"/>`: explicit hard page break inserted by author.
    """
    n = 0
    for el in p_elem.iter():
        local = etree.QName(el).localname
        if local == "lastRenderedPageBreak":
            n += 1
        elif local == "br" and el.get(f"{{{W_NS}}}type") == "page":
            n += 1
    return n


def assign_paragraph_pages(paragraphs) -> tuple[list[int], int]:
    """Return (start_page_per_paragraph_1indexed, total_pages_seen)."""
    page = 1
    starts: list[int] = []
    for p in paragraphs:
        starts.append(page)
        page += count_page_breaks_in_paragraph(p)
    return starts, page


# ---- Segment-aware paragraph translation -----------------------------------
#
# A "segment" is a list of contiguous <w:t> elements within a paragraph that
# should be translated together as one chunk. Splitting on field boundaries,
# tabs, and nested paragraphs preserves Word's layout and field-generated
# values (e.g. TOC page numbers, cross-references, footnote markers).

W_TAG = lambda name: f"{{{W_NS}}}{name}"
P_TAG = W_TAG("p")
T_TAG = W_TAG("t")
TAB_TAG = W_TAG("tab")
BR_TAG = W_TAG("br")
FLDCHAR_TAG = W_TAG("fldChar")
FLDSIMPLE_TAG = W_TAG("fldSimple")
FLDCHARTYPE_ATTR = W_TAG("fldCharType")


def _belongs_to_paragraph(el, p_elem) -> bool:
    """True if el's nearest <w:p> ancestor is p_elem (i.e. not a nested textbox)."""
    a = el.getparent()
    while a is not None:
        if a.tag == P_TAG:
            return a is p_elem
        a = a.getparent()
    return False


def collect_paragraph_segments(p_elem) -> list[list]:
    """Walk paragraph in document order; return list of <w:t> segments to translate.

    Segments are split by:
      - `<w:fldChar>` begin/end boundaries (skips field-displayed text like "8" in PAGEREF)
      - `<w:fldSimple>` element contents (older field format)
      - `<w:tab/>` and `<w:br/>` (preserves tab stops, dot leaders, line breaks)
      - Nested `<w:p>` boundaries (textbox content handled by its own pass)
    """
    segments: list[list] = []
    current: list = []
    field_depth = 0

    # Pre-compute IDs of every element inside any <w:fldSimple> in this paragraph
    fldSimple_inside: set[int] = set()
    for fs in p_elem.iter(FLDSIMPLE_TAG):
        for d in fs.iter():
            fldSimple_inside.add(id(d))

    for el in p_elem.iter():
        if el is p_elem:
            continue
        if not _belongs_to_paragraph(el, p_elem):
            continue

        tag = el.tag
        if tag == FLDCHAR_TAG:
            ftype = el.get(FLDCHARTYPE_ATTR)
            if ftype == "begin":
                if current:
                    segments.append(current)
                    current = []
                field_depth += 1
            elif ftype == "end":
                field_depth = max(0, field_depth - 1)
        elif tag in (TAB_TAG, BR_TAG):
            if current:
                segments.append(current)
                current = []
        elif tag == T_TAG:
            if field_depth > 0 or id(el) in fldSimple_inside:
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(el)

    if current:
        segments.append(current)
    return segments


def segment_text(segment: list) -> str:
    return "".join((t.text or "") for t in segment)


def replace_segment_text(segment: list, new_text: str) -> None:
    """Put translated text into segment's first <w:t>, blank the rest."""
    if not segment:
        return
    segment[0].text = sanitize_for_xml(new_text)
    segment[0].set(XML_SPACE, "preserve")
    for t in segment[1:]:
        t.text = ""
        t.set(XML_SPACE, "preserve")


def translate_word_xml(
    xml_bytes: bytes,
    page_limit: int = 0,
) -> tuple[bytes, list[tuple[str, str, int | None]]]:
    """Translate every translatable segment in a Word XML blob.

    For each `<w:p>`, we split into segments at field/tab/break boundaries so
    that field-generated values (TOC page numbers, cross-references, etc.) are
    preserved. Each segment is translated independently and written back into
    its own first `<w:t>`.

    If `page_limit > 0`, only paragraphs whose start page is `<= page_limit`
    are translated. Page boundaries come from `<w:lastRenderedPageBreak/>` and
    `<w:br w:type="page"/>`. Beyond-limit paragraphs are left in source language.
    """
    tree = etree.fromstring(xml_bytes)
    paragraphs = tree.findall(".//w:p", NSMAP)
    page_starts, total_pages = assign_paragraph_pages(paragraphs)

    # Warn if a page limit was requested but the file has no page-break hints.
    if page_limit > 0 and total_pages == 1 and len(paragraphs) > 0:
        any_breaks = any(count_page_breaks_in_paragraph(p) for p in paragraphs)
        if not any_breaks:
            print(
                "  [warn] page_limit requested but no <w:lastRenderedPageBreak/> "
                "or hard page breaks were found in this XML part. "
                "Open the source DOCX in Word/LibreOffice once and save it to "
                "populate page-break hints. Falling back to translating all paragraphs."
            )

    # Build the work list: (paragraph_idx, segment_idx, text)
    # Cache segments per paragraph so we don't rebuild during the apply step.
    paragraph_segments: list[list[list]] = []
    pending: list[tuple[int, int, str]] = []
    for p_idx, p in enumerate(paragraphs):
        if page_limit > 0 and page_starts[p_idx] > page_limit:
            paragraph_segments.append([])
            continue
        segments = collect_paragraph_segments(p)
        paragraph_segments.append(segments)
        for s_idx, seg in enumerate(segments):
            text = segment_text(seg)
            if is_translatable(text):
                pending.append((p_idx, s_idx, text))

    # Translate concurrently.
    results: dict[tuple[int, int], str] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {
                ex.submit(llm_translate, t): (p_idx, s_idx)
                for p_idx, s_idx, t in pending
            }
            for fut in as_completed(fut_map):
                results[fut_map[fut]] = fut.result()

    # Apply: write translated text back to each segment's first <w:t>.
    pairs: list[tuple[str, str, int | None]] = []
    for p_idx, segments in enumerate(paragraph_segments):
        for s_idx, seg in enumerate(segments):
            key = (p_idx, s_idx)
            if key not in results:
                continue
            original = segment_text(seg)
            translated = results[key]
            if translated != original:
                replace_segment_text(seg, translated)
                pairs.append((original, translated, page_starts[p_idx]))

    return (
        etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True),
        pairs,
    )


def translate_drawing_xml(xml_bytes: bytes) -> tuple[bytes, list[tuple[str, str, int | None]]]:
    """Translate text nodes inside a Word chart or SmartArt diagram XML.

    Targets:
      - `<a:t>` (DrawingML text) — chart titles/labels, SmartArt node text, WordArt.
      - `<c:v>` (chart values) — only translated when alphabetic (skips numeric series).
    """
    tree = etree.fromstring(xml_bytes)
    candidates = []
    for el in tree.iter():
        local = etree.QName(el).localname
        # <a:t> = drawing text (titles, labels). <c:v> = chart values (skip pure numbers).
        if local in ("t", "v") and is_translatable(el.text or ""):
            candidates.append(el)

    pairs: list[tuple[str, str, int | None]] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {ex.submit(llm_translate, e.text): e for e in candidates}
            for fut in as_completed(fut_map):
                el = fut_map[fut]
                original = el.text or ""
                translated = fut.result()
                if translated != original:
                    el.text = sanitize_for_xml(translated)
                    pairs.append((original, translated, None))

    return (
        etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True),
        pairs,
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## DOCX pipeline

# COMMAND ----------

def is_word_text_xml(name: str) -> bool:
    if name == "word/document.xml":
        return True
    return name.startswith(WORD_TEXT_PREFIXES) and name.endswith(".xml")


def is_chart_xml(name: str) -> bool:
    return name.startswith(CHART_PREFIX) and name.endswith(".xml")


def is_diagram_xml(name: str) -> bool:
    """SmartArt data lives at word/diagrams/data*.xml + drawing*.xml."""
    return name.startswith(DIAGRAM_PREFIX) and name.endswith(".xml")


def translate_docx(src_path: str, dst_path: str) -> pd.DataFrame:
    """Read src .docx, translate text in-place at XML level, write dst .docx.
    Returns a DataFrame of (file, section, page, original, translated) rows.

    The page limit (`max_pages`) is applied only to the main body
    (`word/document.xml`). Headers, footers, footnotes and chart text are
    translated in full because they are tied to sections rather than pages
    and are usually small.
    """
    import io as _io
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    rows: list[dict] = []
    src_name = os.path.basename(src_path)

    # Read source into memory once. Volume FUSE on serverless raises
    # OSError [Errno 5] on the random-access reads that zipfile.read() does,
    # but a single full-file read is reliable. Same trick on the output
    # side: build into a buffer, write to the Volume in one shot.
    with open(src_path, "rb") as _fh:
        src_bytes = _fh.read()
    dst_buf = _io.BytesIO()

    with zipfile.ZipFile(_io.BytesIO(src_bytes), "r") as zin, \
         zipfile.ZipFile(dst_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            section = None
            pairs: list[tuple[str, str, int | None]] = []

            if item.filename == "word/document.xml":
                data, pairs = translate_word_xml(data, page_limit=max_pages)
                section = item.filename
            elif is_word_text_xml(item.filename):
                data, pairs = translate_word_xml(data, page_limit=0)
                section = item.filename
            elif is_chart_xml(item.filename) or is_diagram_xml(item.filename):
                data, pairs = translate_drawing_xml(data)
                section = item.filename

            zout.writestr(item, data)

            for orig, tr, page in pairs:
                rows.append({
                    "file": src_name,
                    "section": section,
                    "page": page,
                    "original": orig,
                    "translated": tr,
                })

    with open(dst_path, "wb") as _fh:
        _fh.write(dst_buf.getvalue())

    return pd.DataFrame(rows)


# COMMAND ----------

# MAGIC %md
# MAGIC ## Run translation across all input files

# COMMAND ----------

lang_slug = re.sub(r"[^a-z0-9]+", "_", target_language.lower()).strip("_") or "translated"

per_file_dfs: list[pd.DataFrame] = []
summary_rows: list[dict] = []

for src in files:
    base = os.path.splitext(os.path.basename(src))[0]
    dst = f"{output_dir}/{base}_translated_{lang_slug}.docx"
    print(f"\n→ Translating: {src}")

    skipped_before = _skipped_already_target
    df = translate_docx(src, dst)
    skipped_in_file = _skipped_already_target - skipped_before

    n_changed = len(df)
    n_unique = df["original"].nunique() if n_changed else 0

    # Auto-detect the document's source language from the original spans we
    # just translated. Output is descriptive only — translation itself is
    # language-agnostic (anything not already in the target gets translated).
    src_code, src_name_full = detect_document_language(
        df["original"].tolist() if n_changed else []
    )
    print(
        f"   source={src_name_full} ({src_code})  "
        f"changed={n_changed}  unique={n_unique}  "
        f"skipped_already_{target_lang_code}={skipped_in_file}  →  {dst}"
    )

    df["src_path"] = src
    df["dst_path"] = dst
    df["source_language"] = src_code
    per_file_dfs.append(df)
    summary_rows.append({
        "src": src,
        "dst": dst,
        "source_language_code": src_code,
        "source_language": src_name_full,
        "target_language": target_language,
        "changed": n_changed,
        "unique_changed": n_unique,
        "skipped_already_target": skipped_in_file,
        "cache_hits": n_changed - n_unique,
    })

summary_df = pd.DataFrame(summary_rows)
print("\n=== Summary ===")
display(summary_df)

# Expose the detected source language(s) for the caller (watcher records it in
# bronze_documents). When a batch is single-file (the file-arrival path), this
# is exactly one language.
detected_source_codes = sorted({r["source_language_code"] for r in summary_rows})
detected_source_names = sorted({r["source_language"] for r in summary_rows})

# COMMAND ----------

# MAGIC %md
# MAGIC ## Side-by-side comparison
# MAGIC
# MAGIC Use the table below to spot-check translation quality. The `section` column tells you
# MAGIC which part of the DOCX a span came from (`word/document.xml`, headers, charts, etc).

# COMMAND ----------

if per_file_dfs:
    review_df = pd.concat(per_file_dfs, ignore_index=True)[
        ["file", "section", "page", "original", "translated"]
    ]

    print(
        f"Total {len(review_df)} translated text spans across "
        f"{review_df['file'].nunique()} file(s) and "
        f"{review_df['section'].nunique()} section type(s)."
    )
    if max_pages > 0:
        body_df = review_df[review_df["section"] == "word/document.xml"]
        if len(body_df):
            print(
                f"Body translation limited to first {max_pages} page(s). "
                f"Pages covered in body: "
                f"{int(body_df['page'].min())}–{int(body_df['page'].max())}"
            )
    display(review_df)
else:
    print("No translations produced.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Per-file head + tail spot-check
# MAGIC
# MAGIC Quick visual diff of the first and last few translated spans per file. Easier to scan
# MAGIC than the full table above when you have many documents.

# COMMAND ----------

if per_file_dfs:
    for fname, group in review_df.groupby("file"):
        print(f"\n=== {fname}  —  {len(group)} translated spans ===")
        sample = pd.concat([group.head(5), group.tail(5)]).drop_duplicates().reset_index(drop=True)
        display(sample)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Section breakdown
# MAGIC
# MAGIC Confirms we touched headers/footers/footnotes/charts in addition to the body.

# COMMAND ----------

if per_file_dfs:
    section_breakdown = (
        review_df.groupby(["file", "section"]).size().reset_index(name="spans")
    )
    display(section_breakdown)

# COMMAND ----------

# MAGIC %md
# MAGIC ### XML-illegal character audit
# MAGIC
# MAGIC Tells you whether stray control bytes came from the **source DOCX** (e.g. text pasted
# MAGIC from a PDF, OCR output, non-Microsoft authoring tools) or from the **LLM** (model
# MAGIC occasionally emits stray controls, especially on mixed-script input).
# MAGIC
# MAGIC - `source_*` > 0 → the original file has data-quality issues. Worth flagging upstream.
# MAGIC - `llm_*` > 0 → the model is producing bad bytes; sanitizer is masking it.

# COMMAND ----------

print("XML-illegal character audit:")
print(f"  Source-side  paragraphs with bad chars: {_xml_strip_stats['source_paragraphs_with_bad_chars']}")
print(f"  Source-side  total chars stripped:      {_xml_strip_stats['source_chars_stripped']}")
print(f"  LLM-side     outputs with bad chars:    {_xml_strip_stats['llm_outputs_with_bad_chars']}")
print(f"  LLM-side     total chars stripped:      {_xml_strip_stats['llm_chars_stripped']}")

if _xml_strip_stats["samples_source"]:
    print("\nFirst few SOURCE-side offenders (file already had these illegal bytes):")
    display(pd.DataFrame(_xml_strip_stats["samples_source"]))

if _xml_strip_stats["samples_llm"]:
    print("\nFirst few LLM-side offenders (model produced these illegal bytes):")
    display(pd.DataFrame(_xml_strip_stats["samples_llm"]))

if (_xml_strip_stats["source_chars_stripped"] == 0
        and _xml_strip_stats["llm_chars_stripped"] == 0):
    print("\nNo XML-illegal characters seen on either side. Clean run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation tips
# MAGIC
# MAGIC 1. **Open both files in Word side-by-side.** Use *View → View Side by Side* with synchronous
# MAGIC    scrolling. Confirm page count, section breaks, table layouts, and chart positions match.
# MAGIC 2. **Refresh fields in the translated DOCX.** Press `Ctrl+A` then `F9` (Windows) /
# MAGIC    `Cmd+A` then `F9` (Mac) to refresh the TOC, cross-references, and page numbers — the
# MAGIC    field codes were preserved, but the displayed values need a refresh after text changes.
# MAGIC 3. **Check the section breakdown above.** If you see zero spans from `word/header*` or
# MAGIC    `word/footer*` for a doc that visibly has them in Word, that's a flag to investigate.
# MAGIC 4. **Charts:** if a chart's text didn't change, it's likely a raster image (PNG/EMF
# MAGIC    embedded) rather than a native Word chart. Those need a separate VLM-based pass.
# MAGIC 5. **Hyperlinks:** the URL is preserved; the visible link text is translated and folded
# MAGIC    into the first run of the paragraph. If you need run-level fidelity inside hyperlinks,
# MAGIC    that's a follow-up enhancement (sentinel-tagged run-aware translation).

# COMMAND ----------

# MAGIC %md
# MAGIC ## Return a result payload to the caller
# MAGIC
# MAGIC The watcher reads `source_language_code` to record the auto-detected
# MAGIC source language in `bronze_documents`. JSON so the caller can parse it
# MAGIC with `json.loads(dbutils.notebook.run(...))`.

# COMMAND ----------

import json as _json

_exit_payload = {
    "files": len(files),
    "source_language_codes": detected_source_codes,
    "source_language_names": detected_source_names,
    "source_language_code": detected_source_codes[0] if len(detected_source_codes) == 1 else "mixed",
    "target_language": target_language,
    "target_language_code": target_lang_code,
    "glossary_entries_loaded": _glossary_entry_count,
}
dbutils.notebook.exit(_json.dumps(_exit_payload))