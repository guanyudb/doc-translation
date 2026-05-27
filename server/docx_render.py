"""Render DOCX → HTML with stable per-paragraph indices.

Robust strategy: before letting mammoth convert, we mutate the DOCX so each
<w:p> begins with a sentinel text run like `⫷PIDX:42⫸`. After mammoth
converts to HTML, we find each sentinel, walk up to its enclosing block-ish
element (`<p>`, headings, `<li>`, `<td>`, `<th>`), tag it with `data-pidx`/
`data-page`, and strip the sentinel from the visible text.

Why not just stamp HTML block elements in document order?
Mammoth's emission rules differ between paragraph types — single-paragraph
table cells emit `<td>cell text</td>` (no inner `<p>`), multi-paragraph cells
emit `<p>` per paragraph, headings collapse into `<h1>`–`<h6>`, etc. Any pure
heuristic loses count somewhere; sentinels follow the paragraph wherever
mammoth puts it.
"""
from __future__ import annotations
import io
import re
import zipfile
from dataclasses import dataclass

import mammoth
from lxml import etree, html as lxml_html

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSMAP = {"w": W_NS}
W = "{%s}" % W_NS
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Block elements that should "host" a data-pidx attribute.
BLOCK_HOSTS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th"}

# Sentinel — uses Unicode triangle brackets U+2AF7/U+2AF8, very unlikely to occur
# in real document text. Wrapped pattern is `⫷PIDX:N⫸`.
_SENTINEL_RE = re.compile(r"⫷PIDX:(\d+)⫸")


@dataclass
class Paragraph:
    idx: int
    text: str
    page: int


def _extract_paragraphs_with_pages(docx_bytes: bytes) -> list[Paragraph]:
    """Walk word/document.xml in body order. Page advances on lastRenderedPageBreak
    or hard <w:br w:type='page'/>. Includes paragraphs inside tables."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as z:
        doc_xml = z.read("word/document.xml")

    tree = etree.fromstring(doc_xml)
    body = tree.find("w:body", NSMAP)
    if body is None:
        return []

    out: list[Paragraph] = []
    page = 1
    idx = 0
    for p in body.iter(W + "p"):
        text = "".join(t.text or "" for t in p.findall(".//w:t", NSMAP)).strip()
        out.append(Paragraph(idx=idx, text=text, page=page))
        idx += 1
        for el in p.iter():
            tag = etree.QName(el).localname
            if tag == "lastRenderedPageBreak":
                page += 1
                break
            if tag == "br" and el.get(f"{W}type") == "page":
                page += 1
                break
    return out


def _inject_markers(docx_bytes: bytes) -> bytes:
    """Return new .docx bytes with a sentinel text run prepended to every <w:p>.

    The marker order matches `_extract_paragraphs_with_pages` — both iterate
    body.iter('w:p') in the same order — so idx values stay aligned."""
    in_buf = io.BytesIO(docx_bytes)
    out_buf = io.BytesIO()

    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = _add_markers_to_doc_xml(data)
            zout.writestr(item, data)

    return out_buf.getvalue()


def _add_markers_to_doc_xml(xml_bytes: bytes) -> bytes:
    tree = etree.fromstring(xml_bytes)
    body = tree.find("w:body", NSMAP)
    if body is None:
        return xml_bytes

    idx = 0
    for p in body.iter(W + "p"):
        run = etree.Element(W + "r")
        t = etree.SubElement(run, W + "t")
        t.set(XML_SPACE, "preserve")
        t.text = f"⫷PIDX:{idx}⫸"
        p.insert(0, run)
        idx += 1

    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _find_block_host(el):
    cur = el
    while cur is not None:
        tag = cur.tag.lower() if isinstance(cur.tag, str) else None
        if tag in BLOCK_HOSTS:
            return cur
        cur = cur.getparent()
    return None


def _stamp_html_via_markers(html_str: str, page_by_idx: dict[int, int]) -> str:
    """Find each sentinel in the rendered HTML, stamp its enclosing block-ish element,
    and remove the sentinel text. Operates in-place on the parsed tree."""
    if not html_str.strip():
        return html_str

    fragment = lxml_html.fragment_fromstring(html_str, create_parent="div")

    def stamp_for(host, idx: int):
        if host is None or host.get("data-pidx") is not None:
            return
        host.set("data-pidx", str(idx))
        host.set("data-page", str(page_by_idx.get(idx, 1)))

    # Walk every node and inspect both `text` (content before first child)
    # and each child's `tail` (content after a child element).
    for el in fragment.iter():
        if el.text:
            m = _SENTINEL_RE.search(el.text)
            if m:
                stamp_for(_find_block_host(el), int(m.group(1)))
                el.text = _SENTINEL_RE.sub("", el.text)
        for child in el:
            if child.tail:
                m = _SENTINEL_RE.search(child.tail)
                if m:
                    # tail text logically belongs to `el`, so enclosing block is el itself
                    stamp_for(_find_block_host(el), int(m.group(1)))
                    child.tail = _SENTINEL_RE.sub("", child.tail)

    inner = "".join(lxml_html.tostring(c, encoding="unicode") for c in fragment)
    return inner


def detect_lang(paragraphs: list) -> str:
    """Cheap script-based language guess from a sample of paragraphs.
    Returns ISO-639-1 code where unambiguous, else 'en' as a default."""
    sample = " ".join(
        (p["text"] if isinstance(p, dict) else p.text) or ""
        for p in paragraphs[:30]
    )[:3000]
    if not sample.strip():
        return "en"

    counts = {
        "hira": 0, "kata": 0, "hangul": 0, "kanji": 0,
        "ar": 0, "he": 0, "cyr": 0, "thai": 0,
    }
    for c in sample:
        cp = ord(c)
        if 0x3040 <= cp <= 0x309F: counts["hira"]   += 1
        elif 0x30A0 <= cp <= 0x30FF: counts["kata"]  += 1
        elif 0xAC00 <= cp <= 0xD7AF: counts["hangul"] += 1
        elif 0x4E00 <= cp <= 0x9FFF: counts["kanji"] += 1
        elif 0x0600 <= cp <= 0x06FF: counts["ar"]    += 1
        elif 0x0590 <= cp <= 0x05FF: counts["he"]    += 1
        elif 0x0400 <= cp <= 0x04FF: counts["cyr"]   += 1
        elif 0x0E00 <= cp <= 0x0E7F: counts["thai"]  += 1

    if counts["hira"] or counts["kata"]:
        return "ja"
    if counts["hangul"]:
        return "ko"
    if counts["kanji"]:
        return "zh"
    if counts["ar"]:
        return "ar"
    if counts["he"]:
        return "he"
    if counts["cyr"]:
        return "ru"
    if counts["thai"]:
        return "th"
    return "en"


def render(docx_bytes: bytes) -> tuple[str, list[dict]]:
    """Convert .docx bytes → (html, paragraphs[])."""
    paragraphs = _extract_paragraphs_with_pages(docx_bytes)

    marked_bytes = _inject_markers(docx_bytes)
    result = mammoth.convert_to_html(io.BytesIO(marked_bytes))
    html_str = result.value

    page_by_idx = {p.idx: p.page for p in paragraphs}
    stamped = _stamp_html_via_markers(html_str, page_by_idx)

    return stamped, [{"idx": p.idx, "text": p.text, "page": p.page} for p in paragraphs]


def apply_edits_overlay(html_str: str, edits: dict[int, str]) -> str:
    """Render-time overlay: replace inner content of `[data-pidx="N"]` hosts with
    the reviewer's edited text, leaving all other paragraphs (and outer formatting)
    untouched. Adds `data-edited="1"` so the frontend can show an "edited" pill.

    Edits are treated as plain text — any intra-paragraph formatting on the
    original is intentionally dropped for edited paragraphs only."""
    if not edits or not html_str.strip():
        return html_str

    fragment = lxml_html.fragment_fromstring(html_str, create_parent="div")
    # XPath finds every host in one pass, independent of iteration order, so
    # mutating each element doesn't disturb the traversal.
    for el in fragment.xpath("//*[@data-pidx]"):
        pidx_attr = el.get("data-pidx")
        try:
            idx = int(pidx_attr)
        except (TypeError, ValueError):
            continue
        if idx not in edits:
            continue
        for child in list(el):
            el.remove(child)
        el.text = edits[idx]
        el.set("data-edited", "1")

    return "".join(lxml_html.tostring(c, encoding="unicode") for c in fragment)


def apply_edits_to_docx(docx_bytes: bytes, edits: dict[int, str]) -> tuple[bytes, int]:
    """Mutate the translated .docx so each edited paragraph carries the reviewer's
    text. Returns (new_bytes, edits_applied_count).

    Strategy: walk `body.iter('w:p')` in the same order used by
    `_extract_paragraphs_with_pages` (so paragraph_idx matches), and for any
    paragraph in `edits`, replace all run-bearing children with a single
    `<w:r>` carrying the first original run's `<w:rPr>` and the new text.
    `<w:pPr>` (paragraph properties) and bookmarks are preserved.

    Limitation: edited paragraphs lose intra-paragraph mixed formatting
    (e.g. bold-then-italic-in-one-paragraph collapses to the first run's style).
    Surrounding paragraphs and document structure are untouched."""
    if not edits:
        return docx_bytes, 0

    in_buf = io.BytesIO(docx_bytes)
    out_buf = io.BytesIO()
    applied = 0

    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data, applied = _apply_edits_to_doc_xml(data, edits)
            zout.writestr(item, data)

    return out_buf.getvalue(), applied


def _apply_edits_to_doc_xml(xml_bytes: bytes, edits: dict[int, str]) -> tuple[bytes, int]:
    tree = etree.fromstring(xml_bytes)
    body = tree.find("w:body", NSMAP)
    if body is None:
        return xml_bytes, 0

    # Children of <w:p> we want to delete and replace (anything that holds runs).
    # <w:pPr>, bookmarks, comment markers etc. are kept.
    RUN_HOSTS = {
        f"{W}r", f"{W}hyperlink", f"{W}smartTag",
        f"{W}sdt", f"{W}fldSimple", f"{W}ins", f"{W}del",
    }

    applied = 0
    for idx, p in enumerate(body.iter(W + "p")):
        if idx not in edits:
            continue
        new_text = edits[idx] or ""

        # Capture the first run's <w:rPr> (if any) for formatting continuity.
        first_rpr = None
        first_run = p.find(f"{W}r")
        if first_run is not None:
            rpr = first_run.find(f"{W}rPr")
            if rpr is not None:
                first_rpr = etree.fromstring(etree.tostring(rpr))  # deep copy

        # Remove all run-bearing children.
        for child in list(p):
            if child.tag in RUN_HOSTS:
                p.remove(child)

        # Insert a single new run carrying the edited text.
        new_run = etree.SubElement(p, W + "r")
        if first_rpr is not None:
            new_run.append(first_rpr)
        new_t = etree.SubElement(new_run, W + "t")
        new_t.set(XML_SPACE, "preserve")
        new_t.text = new_text

        applied += 1

    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True), applied
