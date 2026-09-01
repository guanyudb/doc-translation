"""Layout-preserving PDF export for the PDF workflow.

Given the original PDF bytes + its translation artifact (elements carrying a
`bbox` and `target` text) + any reviewer edits, produce a translated PDF that
keeps the original layout: redact each source text run in place — leaving
figures / images / colours / positions untouched — and typeset the final text
back into the SAME box via PyMuPDF `insert_htmlbox` (which reflows and
auto-scales to absorb JA→EN expansion, and renders translated HTML tables
natively).

`ai_parse_document` reports bbox coordinates in pixels at a fixed internal
render DPI; we scale them to PDF points (72 / DPI). PARSE_DPI is a single
tunable constant — validated at ~200 for A4 clinical PDFs.

This is a best-effort visual reconstruction, not pixel-perfect: fonts are
substituted, long translations auto-shrink, and text baked into figure images
stays in the source language (only the separate `caption` element is
translated). Figures are deliberately left as-is.
"""
from __future__ import annotations

import io
import logging
from html import escape

log = logging.getLogger("doc_translation.pdf_layout")

# ai_parse's internal page-render DPI. bbox px -> PDF points = px * 72/DPI.
PARSE_DPI = 200.0
_SCALE = 72.0 / PARSE_DPI

_TAG = {
    "title": "h1", "section_header": "h2", "sub_section_header": "h3",
    "text": "p", "caption": "p", "footnote": "p", "list_item": "li",
}
_FONTSIZE = {"title": 15, "section_header": 12, "sub_section_header": 11}
# Element types whose text we redact + retypeset. Figures are skipped so their
# bitmap (chart / image) survives untouched.
_SKIP_TYPES = {"figure"}


def _final_text(el: dict, edits: dict) -> str:
    """Reviewer edit wins over the machine translation, else fall back to source."""
    edited = edits.get(int(el["id"]))
    if edited is not None:
        return edited
    return el.get("target") or el.get("source") or ""


def _element_html(el: dict, text: str) -> str | None:
    etype = el.get("type", "text")
    if etype == "table":
        return text if text and "<t" in text.lower() else None
    tag = _TAG.get(etype, "p")
    size = _FONTSIZE.get(etype, 10)
    return (
        f"<{tag} style='font-family:sans-serif;font-size:{size}px;margin:0'>"
        f"{escape(str(text))}</{tag}>"
    )


def apply_edits_to_pdf(pdf_bytes: bytes, artifact: dict, edits: dict | None = None) -> bytes:
    """Return translated PDF bytes with source text replaced in place. `edits`
    maps element id -> reviewer text (overrides the machine translation)."""
    import pymupdf

    edits = {int(k): v for k, v in (edits or {}).items()}
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        by_page: dict[int, list[dict]] = {}
        for el in artifact.get("elements", []) or []:
            coord = el.get("bbox")
            if not coord or el.get("type") in _SKIP_TYPES:
                continue
            pg = int(el.get("page", 1)) - 1
            if 0 <= pg < len(doc):
                by_page.setdefault(pg, []).append(el)

        for pg, elements in by_page.items():
            page = doc[pg]
            # 1) redact every source text box (white fill); keep images intact.
            for el in elements:
                x1, y1, x2, y2 = el["bbox"]
                page.add_redact_annot(
                    pymupdf.Rect(x1 * _SCALE, y1 * _SCALE, x2 * _SCALE, y2 * _SCALE),
                    fill=(1, 1, 1),
                )
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
            # Vertical neighbours (distinct box tops, ascending) so an expanded
            # block (EN is longer than JA) can grow into real whitespace but is
            # clamped at the next block's top — it shrinks rather than overlaps.
            tops = sorted({int(el["bbox"][1]) for el in elements})

            def _next_top(y1: float):
                for t in tops:
                    if t > y1 + 1:
                        return t
                return None

            # 2) typeset the final text back into the same boxes, auto-scaled.
            for el in elements:
                text = _final_text(el, edits)
                if not text or not str(text).strip():
                    continue
                html = _element_html(el, text)
                if not html:
                    continue
                x1, y1, x2, y2 = el["bbox"]
                r = pymupdf.Rect(x1 * _SCALE, y1 * _SCALE, x2 * _SCALE, y2 * _SCALE)
                nxt = _next_top(y1)
                ceiling = (nxt * _SCALE - 2) if nxt is not None else (page.rect.y1 - 18)
                bottom = max(r.y1, min(r.y1 + r.height * 0.8, ceiling))
                grow = pymupdf.Rect(r.x0, r.y0, r.x1, bottom)
                try:
                    page.insert_htmlbox(grow, html, scale_low=0.3)
                except Exception:
                    log.warning("insert_htmlbox failed for element %s", el.get("id"),
                                exc_info=True)

        out = io.BytesIO()
        doc.save(out, garbage=3, deflate=True)
        return out.getvalue()
    finally:
        doc.close()
