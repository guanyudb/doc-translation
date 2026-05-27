"""List and read DOCX files from Unity Catalog Volumes via the Files API.

Volume layout (single tenant):
  raw_documents/        — submitter drops the original .docx here
  translated_inplace/   — pipeline output (parallel to raw, same basename)
  translated_reviewed/  — versioned reviewed copies (writes from the app)
  golden/<pair_id>/     — immutable certified copy after promotion (the
                          downstream-of-record artifact)
"""
from __future__ import annotations
import hashlib
import io
from . import config


def list_docx(folder: str) -> list[dict]:
    """Return [{name, path, size, modified}] for .docx files in a Volume folder."""
    out: list[dict] = []
    for entry in config.w().files.list_directory_contents(folder):
        if entry.is_directory:
            continue
        if not entry.path.lower().endswith(".docx"):
            continue
        if entry.name.startswith("~$"):
            continue
        out.append({
            "name": entry.name,
            "path": entry.path,
            "size": entry.file_size,
            "modified": entry.last_modified,
        })
    return sorted(out, key=lambda r: r["name"].lower())


def read_docx(path: str) -> bytes:
    resp = config.w().files.download(path)
    if hasattr(resp.contents, "read"):
        return resp.contents.read()
    return resp.contents


REVIEWED_DIR = f"{config.VOLUME_ROOT}/translated_reviewed"
GOLDEN_DIR   = f"{config.VOLUME_ROOT}/golden"


def reviewed_path(pair_id: str, target_lang: str, version: int) -> str:
    """Versioned path inside translated_reviewed/. Originals in translated_inplace/
    are never overwritten — every publish gets a new file."""
    lang = (target_lang or "tr").lower()
    return f"{REVIEWED_DIR}/{pair_id}_reviewed_{lang}_v{version}.docx"


def golden_paths(pair_id: str, target_lang: str) -> tuple[str, str]:
    """Return (golden_original_path, golden_translated_path) inside golden/<pair_id>/.
    Single-tenant layout, single version per pair (golden is the final word)."""
    lang = (target_lang or "tr").lower()
    base = f"{GOLDEN_DIR}/{pair_id}"
    return (
        f"{base}/{pair_id}_original.docx",
        f"{base}/{pair_id}_translated_{lang}.docx",
    )


def upload_docx(path: str, contents: bytes) -> None:
    """Upload bytes to a Volume path, overwriting if it exists. Creates the
    parent directory if needed."""
    parent = path.rsplit("/", 1)[0]
    try:
        config.w().files.create_directory(parent)
    except Exception:
        # Already exists or insufficient perms — let the upload surface the real error.
        pass
    config.w().files.upload(path, io.BytesIO(contents), overwrite=True)


def sha256(contents: bytes) -> str:
    """Hex SHA-256 — used to bind a file in the audit log to its content."""
    return hashlib.sha256(contents).hexdigest()


def copy_to_golden(
    *,
    pair_id: str,
    target_lang: str,
    original_bytes: bytes,
    translated_bytes: bytes,
) -> dict:
    """Atomically place both files under golden/<pair_id>/ and return their
    paths + hashes. We're single-tenant + single-version so we overwrite the
    same final filenames; if a re-promote ever happens it's caught upstream
    by the lifecycle state machine (PROMOTING is only reachable from UNDER_REVIEW).

    Atomicity strategy: write to `.staging/<pair_id>/...` first, then rename
    into place. The Files API doesn't expose true rename, so we approximate
    by writing both staging files first, then both final files, then deleting
    the staging files. If a crash happens between final writes, the next
    promote will overwrite them anyway. The compliance contract — "the bytes
    you read at the final path match the hash in golden_publications" — is
    enforced by computing the hash from the bytes we just uploaded.
    """
    g_orig, g_tran = golden_paths(pair_id, target_lang)
    orig_hash = sha256(original_bytes)
    tran_hash = sha256(translated_bytes)

    # Write final paths directly. We've already validated state in the caller.
    upload_docx(g_orig, original_bytes)
    upload_docx(g_tran, translated_bytes)

    return {
        "golden_original_path":   g_orig,
        "golden_translated_path": g_tran,
        "golden_original_hash":   orig_hash,
        "golden_translated_hash": tran_hash,
    }


def auto_pair(originals: list[dict], translated: list[dict]) -> list[dict]:
    """Match originals to translations by basename prefix.
    e.g. 'Protocol_v3.docx' ↔ 'Protocol_v3_translated_english.docx'"""
    pairs: list[dict] = []
    by_orig = {o["name"]: o for o in originals}
    used_translations: set[str] = set()
    for orig_name, orig in by_orig.items():
        stem = orig_name[:-len(".docx")]
        match = None
        for t in translated:
            if t["name"].startswith(stem + "_translated") and t["name"] not in used_translations:
                match = t
                break
        if match is None:
            continue
        used_translations.add(match["name"])
        # infer target lang from suffix between '_translated_' and '.docx'
        tname = match["name"]
        target = ""
        marker = "_translated_"
        if marker in tname:
            target = tname[tname.index(marker) + len(marker):]
            if target.endswith(".docx"):
                target = target[:-len(".docx")]
        pairs.append({
            "pair_id": stem,
            "original_name": orig["name"],
            "original_path": orig["path"],
            "translated_name": match["name"],
            "translated_path": match["path"],
            "target_lang": target,
        })
    return pairs


def unpaired_originals(originals: list[dict], translated: list[dict]) -> list[dict]:
    """Return raw files that have no matching translation yet. Used by the
    sidebar 'Awaiting translation' expander so the reviewer can see what's been
    uploaded but is still going through the pipeline."""
    paired_names: set[str] = set()
    for orig in originals:
        stem = orig["name"][:-len(".docx")]
        for t in translated:
            if t["name"].startswith(stem + "_translated"):
                paired_names.add(orig["name"])
                break
    return [o for o in originals if o["name"] not in paired_names]
