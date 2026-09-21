"""Shared LLM client for the app (playground + any in-app translation).

Routes to the right serving path by endpoint FORM:
  * 3-part Unity Catalog name (``catalog.schema.name``, ≥2 dots) → UC AI Gateway
    Responses API (`/ai-gateway/mlflow/v1/responses`).
  * plain serving-endpoint name → chat/completions (`/serving-endpoints/{name}/invocations`).

Both go through the SDK REST client, so the app service principal's auth is handled the
same way. `chat()` returns `(text, usage)` with usage normalized to
`{prompt_tokens, completion_tokens}` across both routes (they name the fields differently).

NOTE: these helpers were lifted out of the legacy `server/pdf_translate.py` (which is slated
for deletion) so nothing new imports from that module. Keep `content_to_text` /
`clean_translation` in sync with the notebooks' local copies (setup/*.py).
"""
from __future__ import annotations

import logging
import re

from . import config

log = logging.getLogger("doc_translation.llm")

MAX_TOKENS = 8192

# Defensive cleanup of model output: a leading ATX markdown header (`# `) and a single pair
# of wrapping quotes. We style headings ourselves, so the model must not add markdown/quotes.
_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s+")


def is_uc_gateway_endpoint(endpoint: str) -> bool:
    """UC AI Gateway endpoints are 3-part Unity Catalog names (catalog.schema.name) served
    via the Responses API. Plain serving-endpoint names never contain dots, so the dot count
    disambiguates the two call paths."""
    return (endpoint or "").count(".") >= 2


def content_to_text(content) -> str:
    """A chat message's content is usually a string, but some models (e.g. Gemini) return a
    list of content blocks [{type, text}]. Normalize to plain text; skip non-text blocks
    (reasoning/tool)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") in ("text", "output_text")
        )
    return ""


def clean_translation(out: str, source: str) -> str:
    """Strip a stray leading markdown header and a single pair of wrapping quotes the model
    sometimes adds. Never let cleanup empty out a non-empty result — fall back to source."""
    if not out:
        return out
    cleaned = _MD_HEADER_RE.sub("", out).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'", "“", "「"):
        inner = cleaned[1:-1].strip()
        if inner:
            cleaned = inner
    return cleaned or source


def _norm_usage(usage) -> dict | None:
    """Normalize a response's usage to {prompt_tokens, completion_tokens}. Serving/chat uses
    prompt_tokens/completion_tokens; the gateway Responses API uses input_tokens/output_tokens.
    Objects (SDK) expose attrs; dicts (REST) expose keys. Returns None if nothing usable."""
    if usage is None:
        return None
    def pick(*names):
        for n in names:
            if isinstance(usage, dict):
                if usage.get(n) is not None:
                    return usage[n]
            elif getattr(usage, n, None) is not None:
                return getattr(usage, n)
        return None
    p = pick("prompt_tokens", "input_tokens")
    c = pick("completion_tokens", "output_tokens")
    if p is None and c is None:
        return None
    return {"prompt_tokens": p, "completion_tokens": c}


def _serving_invoke(model_endpoint: str, body: dict) -> dict:
    """POST to a serving endpoint, with a defensive retry for models that reject our default
    params. Reasoning models (e.g. GPT-5) only accept the default temperature and want
    max_completion_tokens instead of max_tokens; on a 400 naming such a param we drop/rename
    it and retry once, so any chat endpoint the user selects works."""
    path = f"/serving-endpoints/{model_endpoint}/invocations"
    try:
        return config.w().api_client.do("POST", path, body=body)
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
        log.warning("serving endpoint %s rejected a param (%s); retrying without it",
                    model_endpoint, msg[:160])
        return config.w().api_client.do("POST", path, body=retried)


def _serving_chat(model_endpoint: str, system: str, user: str, max_tokens: int) -> tuple[str, dict | None]:
    resp = _serving_invoke(model_endpoint, {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    })
    choices = (resp or {}).get("choices") or []
    if not choices:
        raise RuntimeError(f"serving endpoint '{model_endpoint}' returned no choices: {str(resp)[:200]}")
    text = content_to_text(choices[0].get("message", {}).get("content")).strip()
    return text, _norm_usage((resp or {}).get("usage"))


def _uc_gateway_responses(model: str, system: str, user: str, max_tokens: int) -> tuple[str, dict | None]:
    resp = config.w().api_client.do(
        "POST",
        "/ai-gateway/mlflow/v1/responses",
        body={
            "model": model,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_tokens,
        },
    )
    usage = _norm_usage((resp or {}).get("usage"))
    txt = (resp or {}).get("output_text")
    if txt:
        return txt.strip(), usage
    # Raw JSON has no output_text convenience field — parse the output message items.
    parts = [
        content_to_text(it.get("content"))
        for it in ((resp or {}).get("output") or [])
        if it.get("type") == "message"
    ]
    return "".join(parts).strip(), usage


def chat(model_endpoint: str, system: str, user: str, *, max_tokens: int = MAX_TOKENS) -> tuple[str, dict | None]:
    """Call the configured LLM and return (assistant_text, usage|None). Routes by endpoint
    form (UC gateway vs serving). Raises RuntimeError on an empty response."""
    route = "uc_gateway" if is_uc_gateway_endpoint(model_endpoint) else "serving"
    if route == "uc_gateway":
        text, usage = _uc_gateway_responses(model_endpoint, system, user, max_tokens)
    else:
        text, usage = _serving_chat(model_endpoint, system, user, max_tokens)
    if not text:
        raise RuntimeError(f"model '{model_endpoint}' ({route}) returned no text")
    return text, usage


def route_for(model_endpoint: str) -> str:
    """"serving" | "uc_gateway" — so callers can report which path was exercised."""
    return "uc_gateway" if is_uc_gateway_endpoint(model_endpoint) else "serving"
