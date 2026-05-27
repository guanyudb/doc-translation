"""Reviewer identity from the Databricks Apps runtime.
Apps inject the user's email via the X-Forwarded-Email header.
Streamlit exposes request headers via st.context.headers."""
import streamlit as st


def reviewer() -> str:
    try:
        h = st.context.headers
    except Exception:
        h = {}
    if not h:
        return "local-dev@unknown"
    email = h.get("X-Forwarded-Email") or h.get("x-forwarded-email")
    if email:
        return email
    user = h.get("X-Forwarded-User") or h.get("x-forwarded-user")
    return user or "local-dev@unknown"
