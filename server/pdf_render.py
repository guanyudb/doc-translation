"""Render parsed-PDF translation artifacts → HTML with stable per-element indices.

The PDF workflow's analogue of `docx_render`. `ai_parse_document` returns a list
of elements, each with a reading-order `id`, a `type`, a `page_id`, a `bbox`, and
`content`. The parse+translate job (`setup/pdf_translate.py`) persists a
translation artifact shaped as:

    {
      "source_lang": "ja", "target_lang": "English", "pages": 3,
      "elements": [
        {"id": 0, "type": "title", "page": 1,
         "bbox": [x1, y1, x2, y2], "source": "…", "target": "…"},
        …
      ]
    }

`render()` turns that artifact into the SAME review contract `docx_render.render`
produces — an HTML fragment whose block hosts are stamped `data-pidx` (= element
id) and `data-page`, plus a `paragraphs[]` list of `{idx, text, page}`. Because
the contract is identical, the app's pair-serving, edit-overlay (reused verbatim
from `docx_render.apply_edits_overlay`), and certify paths are format-agnostic:
the review UI cannot tell a PDF pair from a DOCX pair.

Table elements carry an HTML `<table>` string as their content (source and
target each hold a full table). Figures render as a labelled placeholder plus any
OCR'd text (chart labels / captions). Everything else is escaped plain text.
"""
from __future__ import annotations

import re
from html import escape

# ai_parse element type -> HTML block tag used as the data-pidx host.
_TAG = {
    "title": "h1",
    "section_header": "h2",
    "sub_section_header": "h3",
    "text": "p",
    "caption": "p",
    "list_item": "li",
    "footnote": "p",
}
_TABLE_OPEN_RE = re.compile(r"^<table", re.IGNORECASE)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _stamp_table(html_table: str, attrs: str) -> str:
    """Insert data-pidx/data-page onto the opening <table> tag so a click
    anywhere in the table resolves (via closest [data-pidx]) to this element."""
    t = html_table.strip()
    if _TABLE_OPEN_RE.match(t):
        return _TABLE_OPEN_RE.sub(f"<table{attrs}", t, count=1)
    return f"<table{attrs}>{t}</table>"


def _table_to_text(html_table: str) -> str:
    """Flatten a table's cells to ` | `-joined text for the side panel. The
    document pane still renders the full HTML table; this is only the compact
    text the reviewer sees/edits in the paragraph panel."""
    cells = _CELL_RE.findall(html_table or "")
    flat = [_TAG_STRIP_RE.sub("", c).strip() for c in cells]
    return " | ".join(c for c in flat if c)


def _block_html(el: dict, text: str) -> str:
    pidx = int(el["id"])
    page = int(el.get("page", 1))
    etype = el.get("type", "text")
    attrs = f' data-pidx="{pidx}" data-page="{page}"'

    if etype == "table":
        return _stamp_table(text or "", attrs)

    if etype == "figure":
        body = escape(text or "").strip()
        inner = f'<figcaption class="pdf-figure-text">{body}</figcaption>' if body else ""
        return (
            f'<figure{attrs} class="pdf-figure">'
            f'<div class="pdf-figure-mark">\U0001F5BC Figure</div>{inner}</figure>'
        )

    tag = _TAG.get(etype, "p")
    return f'<{tag}{attrs} class="pdf-{etype}">{escape(text or "")}</{tag}>'


def _text_for(el: dict, side: str) -> str:
    """Element text for a side, falling back to source when target is missing
    (e.g. an element that wasn't translatable)."""
    return el.get(side) or el.get("source") or ""


def render(artifact: dict, side: str = "target") -> tuple[str, list[dict]]:
    """`(artifact, side in {'source','target'})` -> `(html, paragraphs[])`.

    Mirrors `docx_render.render`'s return contract exactly. Overlay reviewer
    edits by passing the returned html through
    `docx_render.apply_edits_overlay(html, edits)` — no PDF-specific overlay is
    needed because the data-pidx host convention is shared.
    """
    elements = artifact.get("elements", []) or []
    blocks: list[str] = []
    paragraphs: list[dict] = []
    for el in elements:
        text = _text_for(el, side)
        blocks.append(_block_html(el, text))
        ptext = _table_to_text(text) if el.get("type") == "table" else text
        paragraphs.append(
            {"idx": int(el["id"]), "text": ptext, "page": int(el.get("page", 1))}
        )
    return "\n".join(blocks), paragraphs


def detect_lang_from_artifact(artifact: dict) -> str:
    """Source language recorded by the parse+translate job, else script-detect
    from the source text (reuses docx_render's detector to stay consistent)."""
    lang = (artifact.get("source_lang") or "").strip()
    if lang:
        return lang
    from server import docx_render

    src_paras = [
        {"text": el.get("source", "")} for el in artifact.get("elements", []) or []
    ]
    return docx_render.detect_lang(src_paras)
