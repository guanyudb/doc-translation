# Doc Translation Review Platform

A compliance-grade, end-to-end pipeline + reviewer app for translating
clinical/regulated `.docx` documents on Databricks.

A submitter drops a Japanese (or any source-language) `.docx` into a Unity
Catalog Volume. A file-arrival-triggered Lakeflow job translates it in-place
at the OOXML level with Foundation Model API. Reviewers certify it
paragraph-by-paragraph in a Streamlit app. Once 100% certified, the document
is atomically promoted to a "golden" Volume location, locked read-only, and
mirrored to Delta for long-term archive.

Every action is audited. Every certified document is content-addressed.
Once locked, writes are refused with an explicit `PairLockedError` (which is
itself audited).

---

## Architecture at a glance

```
[Raw Volume]  →  [File-arrival trigger]  →  [Translation Job]  →  [Translated Volume]
                          │                         │                      │
                          ▼                         ▼                      ▼
                  bronze_documents          claude-sonnet-4-6        translated_inplace/
                  (status=TRANSLATING)      (in-place OOXML edit)    status=TRANSLATED
                          │                         │                      │
                          └─────────────────────────┴──────┬───────────────┘
                                                          ▼
                                          [ Lakebase Postgres — live review ]
                                                          │
                                                          ▼
                                          [ Reviewer App (Databricks Apps) ]
                                                          │
                                              certify ▼ edit ▼ publish
                                                          │
                                                          ▼
                                                [Golden Volume]
                                              golden_publications
                                              SHA-256 + immutable
                                                          │
                                                          ▼
                                                [ Delta mirror ]
                                          audit_events · golden_publications
                                          silver_review_snapshots · bronze_documents
```

**Two storage planes:**
- **Lakebase Postgres** — hot OLTP for the reviewer app (low-latency, transactional)
- **Delta Lake** — long-term archive and BI surface (append-only audit, 7-year retention)

Data flows from Lakebase → Delta only at promotion time, so the hot path stays fast and the cold path stays compliance-grade.

---

## Lifecycle state machine

| State | Meaning | Allowed transitions |
|---|---|---|
| `UNDER_REVIEW` | Default — edits + status changes allowed | `PROMOTING` |
| `PROMOTING` | Lock-in in flight (file copy + Delta sync) | `PUBLISHED`, `UNDER_REVIEW` (on failure) |
| `PUBLISHED` | Golden zone copy exists, doc is read-only | `ARCHIVED` |
| `ARCHIVED` | Retention started, terminal | — |

Writes against `PUBLISHED` docs are rejected at the store layer with `PairLockedError` and audited as `INVALID_WRITE_BLOCKED`.

---

## End-to-end workflow (5 phases)

```
   UPLOAD              AUTO-TRANSLATE          REVIEW                  PROMOTE                 ARCHIVE
─────────────       ─────────────────────   ──────────────         ───────────────────     ───────────────────
Drop .docx into     Job fires via Lakeflow  App sidebar shows      Header chip flips       Certified DOCX in
raw_documents/      file-arrival trigger    the new pair after     UNDER REVIEW →          golden/<pair>/
                                            translation lands      PUBLISHED at 100%       + read-only mode
                                                                   certified
bronze_documents    claude-sonnet-4-6       OPENED + PARAGRAPH_*   GOLD_PROMOTED event     Delta mirror:
row inserted        writes in-place OOXML   audit events per       golden_publications     audit_events
status=TRANSLATING  to translated_inplace/  reviewer action        row (SHA-256 hashed)    golden_publications
                    status=TRANSLATED                                                      silver_review_snapshots
```

---

## Reviewer workflow (what the user does)

1. **Open the app** → pick a pair from the sidebar dropdown (with live search filter)
2. **Read the side-by-side panes** (original left, translated right). Click any paragraph to focus it in the right rail.
3. **For each paragraph** in the rail editor:
   - Edit the translation if needed → `✏️ Save edit` (writes overlay to Lakebase, appends history row, pane re-renders with the edit highlighted)
   - Choose `● Pending` / `✓ Certify` / `⚑ Flag`
   - Optionally add a comment → `Save`
4. **Bulk-certify** to move faster:
   - `✓ Certify page · N paragraphs` — current page only
   - `✓✓ Certify whole doc · N remaining` — everything not yet certified
   - **`Skip hi-conf (≥ 0.9)`** checkbox — when on, the ←→ nav skips paragraphs that are high-confidence AND already certified
5. **If any edits**: header shows `⤴ Publish · N edits` → opens dialog → diff preview → writes a versioned `<file>_reviewed_<lang>_v<N>.docx` to `translated_reviewed/` (audited in `review_publish_log`)
6. **When 100% certified + no flagged + no unpublished edits**: header swaps to `🏅 Promote to Gold` → confirm-by-typing-pair-id → atomically copies both files (with SHA-256) to `/golden/<pair_id>/`, locks the doc, syncs Delta. Read-only thereafter.
7. **Audit anytime**: sidebar `📜 Audit trail` shows every event with actor + timestamp; `🏅 Golden publication` shows the certified paths + hashes; `📦 Delta mirror synced at …` confirms the long-term archive.

---

## Intelligence (Loop 1)

The platform learns from reviewer behavior in two cheap, no-LLM ways:

**Per-paragraph confidence score** — computed at first render of each pair, persisted to `paragraph_confidence`. Combines:
- `length_ratio` — target/source length vs. expected band for the language pair
- `untranslated_pct` — fraction of source-script characters still in the target (catches passthrough)
- `repeated_ngrams` — 5-token n-grams repeating ≥ 3× (catches model loops)

Combined via weighted geometric mean so any single red flag drags the score down. Surfaced as a colored pill on each paragraph and as `N hi-conf` / `N lo-conf` chips in the header progress strip.

**Glossary mining** — `translation_glossary` table is populated by scanning `review_edit_history` for repeated (model output → reviewer correction) patterns across documents and reviewers. Surfaced in a sidebar drawer. Phase 1c (deferred) will inject the top-N entries into the FMAPI system prompt at translation time so the same correction doesn't have to be made twice.

**Confidence is a triage signal, not a quality guarantee.** The reviewer remains the source of truth — the score just helps them prioritize.

---

## Compliance & traceability

- **Append-only** `audit_events` Delta table — INSERT-only ACL for the app SP, 7-year retention (`delta.deletedFileRetentionDuration = 'interval 2557 days'`)
- **Content-addressed** golden files — SHA-256 of original + translated stored in `golden_publications`
- **Locked after publish** — writes refused with `PairLockedError`; the blocked attempt itself emits an `INVALID_WRITE_BLOCKED` event
- **Every event carries** actor (SSO email or SP id) + timestamp (DB clock) + event_type + before/after JSON + correlation_id + paragraph_idx + client_ip
- **Lakebase → Delta sync** at promotion: `audit_events`, `golden_publications`, `silver_review_snapshots` mirrored

**"Produce full history for any document in under 5 minutes"** — one SQL query against the four Delta tables, filtered by `pair_id`.

---

## Components

| Layer | What | Where |
|---|---|---|
| Storage (files) | Original / translated / reviewed / golden `.docx` | Unity Catalog Volume `hls_amer_catalog.guanyu_chen.doc-translation` |
| Storage (state) | Live review state — pairs, feedback, edits, audit, glossary, confidence | Lakebase Postgres `lakebasepoc`, schema `doc_translation` |
| Storage (archive) | Long-term audit + publication archive | Delta tables in `hls_amer_catalog.guanyu_chen` |
| Translation | In-place OOXML translation per paragraph | FMAPI `databricks-claude-sonnet-4-6` |
| Orchestration | File-arrival → translation pipeline | Lakeflow Job `doc-translation-pipeline` |
| Review UI | Side-by-side pane viewer, paragraph rail, certify/edit/publish/promote | Streamlit on Databricks Apps |
| Auth | Reviewer identity via `X-Forwarded-Email`, SP for system actions | Databricks Apps SSO + Service Principal |

---

## Repository layout

```
doc-translation-app/
├── app.py                          # Streamlit entrypoint
├── app.yaml                        # Databricks Apps runtime config
├── requirements.txt                # streamlit, psycopg[binary,pool], databricks-sdk, mammoth, lxml, pymupdf
├── pyproject.toml
├── server/
│   ├── auth.py                     # Reviewer from X-Forwarded-Email
│   ├── config.py                   # env + WorkspaceClient singleton
│   ├── confidence.py               # Heuristic per-paragraph scoring (no LLM)
│   ├── db.py                       # Lakebase psycopg pool with OAuthConnection
│   ├── delta_sync.py               # Lakebase → Delta mirror at promotion
│   ├── docx_render.py              # DOCX → HTML via sentinel markers + lxml
│   ├── glossary.py                 # Mine review_edit_history for correction patterns
│   ├── store.py                    # Lakebase CRUD + audit emits + lifecycle
│   ├── styles.py                   # Editorial-palette CSS for the app
│   └── volume.py                   # UC Volume listing, DOCX read, golden promotion
├── components/
│   └── dual_pane/
│       ├── __init__.py             # Custom Streamlit component declaration
│       └── frontend/index.html     # Vanilla HTML/CSS/JS dual-pane viewer
└── docs/
    ├── architecture.png            # Architecture diagram (PNG)
    ├── architecture.svg            # Architecture diagram (SVG)
    └── pipeline_design.md          # Phase 1+ design doc
```

The translation orchestration lives outside the app, as Databricks notebooks under `/Users/guanyu.chen@databricks.com/Translation PoC/`:
- `DOCX Inplace Translation` — the per-file translator (FMAPI + lxml in-place edit)
- `Auto-Translate Watcher` — the orchestration wrapper that scans `raw_documents/`, skips already-translated, writes `bronze_documents` rows, invokes the translator per file

---

## Deploying

```bash
# Sync local working copy to the Workspace path
databricks sync . /Workspace/Users/<you>/databricks_apps/doc-translation \
  --full --exclude __pycache__ --exclude .gitignore --exclude .venv \
  --profile <profile>

# Trigger a deployment
databricks apps deploy doc-translation \
  --source-code-path /Workspace/Users/<you>/databricks_apps/doc-translation \
  --no-wait --profile <profile>
```

App URL after deploy: `https://doc-translation-<random>.aws.databricksapps.com`

---

## Schema migrations

DDL runs as the table owner (not the SP, which has only INSERT/UPDATE/DELETE on the data tables):

```bash
DATABRICKS_PROFILE=<profile> PGHOST=... LAKEBASE_INSTANCE=... \
  PGDATABASE=... PGSSLMODE=require PGSCHEMA=doc_translation \
  VOLUME_ROOT=... \
  .venv/bin/python -c "from server import store; from server.db import pool; pool.open(wait=True); store.ensure_schema()"
```

Delta mirror tables are created by `server/delta_sync.ensure_delta_schema()` (called lazily by the SP via its SQL warehouse).

---

## Local development

Requires arm64 Python (x86_64 venv breaks `cryptography`'s `_cffi_backend` import on Apple Silicon).

```bash
python3.10 -m venv .venv  # /opt/homebrew/bin/python3.10 on macOS
.venv/bin/pip install -r requirements.txt

DATABRICKS_PROFILE=<profile> \
PGHOST=instance-...database.cloud.databricks.com \
PGPORT=5432 PGDATABASE=databricks_postgres PGSSLMODE=require \
PGSCHEMA=doc_translation LAKEBASE_INSTANCE=lakebasepoc \
VOLUME_ROOT=/Volumes/.../doc-translation \
DATABRICKS_WAREHOUSE_ID=<warehouse-id> \
DELTA_CATALOG=hls_amer_catalog DELTA_SCHEMA=guanyu_chen \
.venv/bin/streamlit run app.py \
  --server.port=8501 --server.address=0.0.0.0 --server.headless=true
```

---

## What's deferred

- **Phase 1c — glossary injection** into the translation prompt (one-line read of `glossary.glossary_for_prompt(...)` in the inner notebook's system prompt; deferred until live data has a few dozen entries to validate against)
- **PDF support** — designed but not built; would need `pymupdf` extraction + an "overlay vs. in-place" writeback choice per document
- **Two-eyes / multi-reviewer attestation** before promotion
- **AI/BI dashboards** on the Delta tables (pipeline health, review backlog, SLA breach)
- **Lakeflow alerts** on `FAILED_TRANSLATION` / `DELTA_SYNC_FAILED` events
