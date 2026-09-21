"""Prompt playground — translate one pasted paragraph with a chosen/edited prompt so
authors can iterate on prompts without uploading a document and waiting for the pipeline.

Language injection mirrors the notebooks but is placeholder-optional: substitute
`{lang}`/`{target_lang}` (and the new `{source_lang}`) when present; otherwise append an
explicit language directive so a prompt with no token still translates. The caller (the
UI) always renders the exact `effective_prompt` returned here.

Glossary matching is replicated from the DOCX notebook (target-only, dual-form name+code,
longest-wins, capped) rather than reusing `glossary.glossary_for_prompt` (which is
source-filtered + exact-match and would inject a different subset than the pipeline).
"""
from __future__ import annotations

import time

from . import glossary as glossary_mod
from . import llm

MAX_SOURCE_CHARS = 10_000
MAX_GLOSSARY_INJECT = 20   # match the notebook cap

# 3rd copy of this map (server/playground.py, setup/docx_inplace_translation.py:192,
# setup/pdf_inplace_translation.py). KEEP IN SYNC — used to match the notebook's dual-form
# (name + ISO code) glossary target filter.
LANG_NAME_TO_CODE = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
    "polish": "pl", "turkish": "tr", "arabic": "ar", "hindi": "hi",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
    "japanese": "ja", "korean": "ko", "chinese": "zh-cn",
    "simplified chinese": "zh-cn", "traditional chinese": "zh-tw",
}

# Verbatim from the DOCX notebook's _system_prompt_for (:552-556) — the conditional wording
# ("when the source contains…") makes a per-request UNION correct for every segment.
GLOSSARY_BLOCK_HEADER = (
    "\n\nGLOSSARY — when the source contains the following terms, use the "
    "specified translation verbatim (these are approved, required "
    "terminology; they override your default word choice):\n"
)

# Appended when a prompt has no target-language token, so language is always injected.
LANG_DIRECTIVE = "\n\nTranslate the user's text into {target}. The source language is {source}."
LANG_DIRECTIVE_NO_SOURCE = "\n\nTranslate the user's text into {target}."


def effective_system_prompt(base_body: str, source_lang: str | None, target_lang: str) -> dict:
    """Resolve the prompt body into the exact system prompt the model will see.

    Substitutes `{target_lang}`/`{lang}` → target and `{source_lang}` → source when present
    (str.replace, so literal braces in the body are safe). If NO target token is present,
    appends an explicit directive (carrying both source and target when the source is known).
    Returns {system, used_lang_token, used_source_lang_token, directive_appended}."""
    body = base_body or ""
    target = (target_lang or "").strip()
    source = (source_lang or "").strip()
    has_target_token = ("{lang}" in body) or ("{target_lang}" in body)
    has_source_token = "{source_lang}" in body

    system = body.replace("{target_lang}", target).replace("{lang}", target)
    system = system.replace("{source_lang}", source or "the source language")

    directive_appended = not has_target_token
    if directive_appended:
        if source:
            system += LANG_DIRECTIVE.format(target=target, source=source)
        else:
            system += LANG_DIRECTIVE_NO_SOURCE.format(target=target)

    return {
        "system": system,
        "used_lang_token": has_target_token,
        "used_source_lang_token": has_source_token,
        "directive_appended": directive_appended,
    }


def _approved_pairs_for_target(target_name: str) -> list[tuple[str, str]]:
    """Approved glossary pairs whose target matches (dual-form: language name OR ISO code,
    lowercased) — the same filter the notebooks apply. One `list_glossary` read (cap 200)."""
    target = (target_name or "").strip().lower()
    code = LANG_NAME_TO_CODE.get(target, target[:2])
    forms = {target, code}
    out: list[tuple[str, str]] = []
    for e in glossary_mod.list_glossary(approved_only=True, limit=200):
        if (e.get("target_lang") or "").strip().lower() in forms:
            mp = (e.get("model_phrase") or "").strip()
            corr = (e.get("correction") or "").strip()
            if len(mp) >= 2 and corr:
                out.append((mp, corr))
    return out


def _match_glossary(text: str, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pairs whose source phrase occurs in `text` — longest-wins dedupe (drop a phrase that
    is a substring of a longer matched phrase), capped. Mirrors the notebook's
    `glossary_matches`, minus the Aho-Corasick dependency (a substring scan over a small,
    capped candidate list is fine here)."""
    if not text or not pairs:
        return []
    hits = {mp: corr for mp, corr in pairs if mp in text}
    if not hits:
        return []
    ordered = sorted(hits.items(), key=lambda kv: -len(kv[0]))
    kept: list[tuple[str, str]] = []
    for mp, corr in ordered:
        if any(mp in longer for longer, _ in kept):
            continue
        kept.append((mp, corr))
    return kept[:MAX_GLOSSARY_INJECT]


def run_translation(*, prompt_body: str, source_text: str, source_lang: str | None,
                    target_lang: str, model_endpoint: str) -> dict:
    """Build the effective system prompt (+ matched glossary), call the model, clean up, and
    return everything the UI needs to render + explain the result. Raises on model failure."""
    eff = effective_system_prompt(prompt_body, source_lang, target_lang)
    matched = _match_glossary(source_text, _approved_pairs_for_target(target_lang))
    system = eff["system"]
    if matched:
        system += GLOSSARY_BLOCK_HEADER + "\n".join(f'- "{s}" → "{t}"' for s, t in matched)

    t0 = time.time()
    text, usage = llm.chat(model_endpoint, system, source_text)
    text = llm.clean_translation(text, source_text)
    elapsed_ms = int((time.time() - t0) * 1000)

    return {
        "translation": text,
        "effective_prompt": system,
        "used_lang_token": eff["used_lang_token"],
        "used_source_lang_token": eff["used_source_lang_token"],
        "directive_appended": eff["directive_appended"],
        "glossary_terms": [{"source": s, "target": t} for s, t in matched],
        "route": llm.route_for(model_endpoint),
        "model_endpoint": model_endpoint,
        "elapsed_ms": elapsed_ms,
        "usage": usage,
    }
