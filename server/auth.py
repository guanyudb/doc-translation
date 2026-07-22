"""Reviewer identity from the Databricks Apps runtime.

Apps inject the signed-in user's email via the `X-Forwarded-Email` header on
every request. This module resolves it in two runtimes:

* FastAPI (server_api.py) — a middleware calls `set_request_headers(...)` per
  request, stashing the headers in a contextvar. `reviewer()` reads that.
* Streamlit (legacy/streamlit_app.py) — falls back to `st.context.headers`.

Either way, `reviewer()` returns the email or a safe local-dev default.
"""
from __future__ import annotations
import contextvars

# Set per-request by the FastAPI middleware. Empty dict when unset.
_request_headers: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "request_headers", default={}
)


def set_request_headers(headers: dict) -> None:
    """Called by the FastAPI middleware with the current request's headers
    (lower-cased keys recommended)."""
    _request_headers.set(headers or {})


def _from_headers(h: dict) -> str | None:
    if not h:
        return None
    email = h.get("X-Forwarded-Email") or h.get("x-forwarded-email")
    if email:
        return email
    user = h.get("X-Forwarded-User") or h.get("x-forwarded-user")
    return user or None


def reviewer() -> str:
    # 1. FastAPI request context (set by middleware).
    who = _from_headers(_request_headers.get())
    if who:
        return who

    # 2. Streamlit context (legacy app).
    try:
        import streamlit as st
        who = _from_headers(dict(st.context.headers))
        if who:
            return who
    except Exception:
        pass

    return "local-dev@unknown"
