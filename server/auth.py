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
import os

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


def _admin_set() -> set[str]:
    """Configured admin emails, lowercased. Sourced from APP_ADMIN_EMAILS
    (the `admin_emails` secret — the deployer by default), comma/semicolon
    separated."""
    raw = os.environ.get("APP_ADMIN_EMAILS") or ""
    return {e.strip().lower() for e in raw.replace(";", ",").split(",") if e.strip()}


def is_admin() -> bool:
    """Whether the signed-in reviewer is an app admin (may change Settings).

    Admin is gated to APP_ADMIN_EMAILS. When it's unset/blank, admin is OPEN —
    so pre-existing deployments and local dev aren't locked out before the
    `admin_emails` secret is seeded. Fresh deploys seed it to the deploying
    user, so they're gated to the deployer by default."""
    admins = _admin_set()
    if not admins:
        return True
    return reviewer().strip().lower() in admins
