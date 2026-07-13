import os
import streamlit.components.v1 as components

_path = os.path.join(os.path.dirname(__file__), "frontend")
_component = components.declare_component("dual_pane", path=_path)


def dual_pane(
    *,
    orig_html: str,
    tran_html: str,
    orig_paragraphs: list[dict],
    tran_paragraphs: list[dict],
    feedback: dict,
    active_idx: int | None,
    source_lang: str,
    target_lang: str,
    height: int = 880,
    key: str = "dual_pane",
) -> dict | None:
    """Returns None on first render, then {'type':'active','idx':N,'ts':...} on click."""
    return _component(
        orig_html=orig_html,
        tran_html=tran_html,
        orig_paragraphs=orig_paragraphs,
        tran_paragraphs=tran_paragraphs,
        feedback=feedback,
        active_idx=active_idx,
        source_lang=source_lang,
        target_lang=target_lang,
        height=height,
        key=key,
        default=None,
    )
