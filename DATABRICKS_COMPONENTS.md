# Databricks Components in `doc-translation` App

A reference for what each Databricks platform component is doing in this app.

---

## 1. Databricks App (Hosting)

- **Name:** `doc-translation` on `fe-vm-hls-amer.cloud.databricks.com` (CLI profile `vlm`)
- **App ID / SP client ID:** `27a20802-3808-428b-8a6c-23bc0bd02ee6`
- **Public URL:** `https://doc-translation-1602460480284688.aws.databricksapps.com`
- **Runtime:** Streamlit (MEDIUM compute), launched via `app.yaml` → `streamlit run app.py --server.port=8000 --server.address=0.0.0.0 --server.headless=true`
- **Source path:** `/Workspace/Users/guanyu.chen@databricks.com/databricks_apps/doc-translation`
- **Reviewer identity:** runtime injects `X-Forwarded-Email` header → `server/auth.py` reads via `st.context.headers` so feedback rows record the actual logged-in user
- **Deploy:** `databricks sync` then `databricks apps deploy doc-translation --source-code-path <ws-path>`

---

## 2. Lakebase (Postgres) — Persistence

- **Instance:** `lakebasepoc` (shared, owner suryasai.turaga); UID `6b59171b-cee8-4acc-9209-6c848ffbfbfe`
- **Host:** `instance-6b59171b-…database.cloud.databricks.com:5432`, DB `databricks_postgres`, schema `doc_translation`

**Tables:**

```sql
review_pairs (
  pair_id          TEXT PRIMARY KEY,
  original_path    TEXT NOT NULL,
  translated_path  TEXT NOT NULL,
  source_lang      TEXT,
  target_lang      TEXT,
  total_paragraphs INT,
  created_at       TIMESTAMPTZ DEFAULT now(),
  finalized_at     TIMESTAMPTZ
);

review_feedback (
  pair_id        TEXT REFERENCES review_pairs(pair_id) ON DELETE CASCADE,
  paragraph_idx  INT  NOT NULL,
  status         TEXT DEFAULT 'pending' CHECK (status IN ('pending','certified','flagged')),
  comment        TEXT,
  reviewer       TEXT,
  updated_at     TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (pair_id, paragraph_idx)
);
CREATE INDEX review_feedback_status_idx ON review_feedback (pair_id, status);
```

**Roles:** developer (`guanyu.chen@databricks.com`) and app SP both granted `DATABRICKS_SUPERUSER` via `WorkspaceClient.database.create_database_instance_role`. SP also granted table-level `SELECT/INSERT/UPDATE/DELETE` plus default privileges in `doc_translation`.

**Connection (`server/db.py`):** `psycopg[binary,pool].ConnectionPool` with custom `OAuthConnection` subclass that calls `w().database.generate_database_credential(instance_names=["lakebasepoc"])` per new connection. `min_size=1, max_size=8, max_lifetime=2700s` to recycle before the 1-hour OAuth token expires. Pool opened once in `app.py`.

**Operations (`server/store.py`):** `upsert_pair`, `list_pairs_with_progress`, `get_feedback`, `upsert_feedback`, `bulk_upsert_feedback` (page-level certify with optional skip-commented), `progress_for(pair_id)`.

---

## 3. Unity Catalog Volume — File Storage

- **Volume:** `hls_amer_catalog.guanyu_chen.doc-translation` (managed, S3-backed)
- **POSIX root:** `/Volumes/hls_amer_catalog/guanyu_chen/doc-translation`

**Layout:**

- `raw_documents/` — original `.docx` (Japanese in PoC)
- `translated_inplace/` — pipeline output `<basename>_translated_<lang>.docx`

**Pairing (`server/volume.py:auto_pair`):** scans both folders via `WorkspaceClient.files.list_directory_contents`, matches by basename + `_translated_` suffix, skips Word lock files (`~$…`), extracts target language from filename suffix.

**Reads:** `WorkspaceClient.files.download(path).contents`, results cached via `@st.cache_data(ttl=600)`.

**Required UC grants for SP:**

```
USE CATALOG  on hls_amer_catalog
USE SCHEMA   on hls_amer_catalog.guanyu_chen
READ_VOLUME, WRITE_VOLUME on hls_amer_catalog.guanyu_chen.doc-translation
```

---

## 4. Databricks SDK Patterns (`server/config.py`)

- **Dual-mode auth:** `WorkspaceClient()` in Apps runtime (auto-detects via `DATABRICKS_APP_NAME`), `WorkspaceClient(profile="vlm")` locally; wrapped in `w()` singleton
- **Lakebase OAuth:** per-connection token via `database.generate_database_credential`; pool lifetime ensures rotation before 1h expiry
- **Files API:** `list_directory_contents` for discovery, `download` for byte-level DOCX reads

---

## 5. Foundation Model API (upstream pipeline, separate notebook)

Notebook: `/Users/guanyu.chen@databricks.com/Translation PoC/DOCX Inplace Translation` — not part of the app itself, but produces the files the app reads.

- Uses `WorkspaceClient.serving_endpoints.get_open_ai_client()` against `databricks-gemini-3-1-pro`
- In-place OOXML `<w:t>` translation via `lxml` (preserves layout, charts, headers/footers, footnotes, SmartArt)
- ThreadPoolExecutor + MD5 paragraph cache; page-bounded via `<w:lastRenderedPageBreak/>` / `<w:br w:type="page"/>`
- Output written to `translated_inplace/`, picked up by the app

---

## 6. End-to-End Flow

1. SA uploads `Foo.docx` → `raw_documents/`
2. Translation notebook → writes `Foo_translated_english.docx` → `translated_inplace/`
3. App auto-pairs via Files API listing
4. On selection: app downloads both DOCXs → `docx_render.render` injects `⫷PIDX:N⫸` sentinels into each `<w:p>`, runs mammoth, walks the rendered tree to stamp `data-pidx`/`data-page` on block hosts (`p`, `h1-6`, `li`, `td`, `th`)
5. Custom Streamlit component (`components/dual_pane`) renders both panes; clicks post `{type:'active', idx, ts}` via `postMessage`
6. Certify/flag/comment writes go through `psycopg` pool → progress + minimap update next render
7. Source language auto-detected via script-based heuristic in `docx_render.detect_lang` (JA/KO/ZH/AR/HE/RU/TH/EN)

---

## 7. Local Dev

- arm64 venv (`/opt/homebrew/bin/python3.10`) required — x86_64 venv breaks `cryptography` (`_cffi_backend` arch mismatch)
- Internal PyPI proxy: `https://pypi-proxy.dev.databricks.com/simple`
- Run:

```bash
DATABRICKS_PROFILE=vlm PGHOST=… LAKEBASE_INSTANCE=lakebasepoc \
  VOLUME_ROOT=/Volumes/hls_amer_catalog/guanyu_chen/doc-translation \
  PGSCHEMA=doc_translation \
  .venv/bin/streamlit run app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true
```

---

## Key Files

| File | Role |
|---|---|
| `app.py` | Streamlit entrypoint — sidebar, header, dual-pane layout, rail, callbacks |
| `server/config.py` | Env config + `WorkspaceClient` singleton |
| `server/db.py` | Lakebase psycopg pool + `OAuthConnection` |
| `server/volume.py` | UC Volume listing, DOCX read, auto-pair |
| `server/docx_render.py` | DOCX→HTML via sentinel markers + mammoth + lxml; lang detect |
| `server/store.py` | Postgres CRUD + bulk page-level certify |
| `server/auth.py` | Reviewer from `X-Forwarded-Email` |
| `server/styles.py` | Editorial palette CSS |
| `components/dual_pane/__init__.py` | Custom Streamlit component declaration |
| `components/dual_pane/frontend/index.html` | Vanilla HTML/CSS/JS dual-pane viewer |
| `app.yaml` | Apps runtime command + env vars |
| `requirements.txt` | streamlit, psycopg[binary,pool], databricks-sdk, mammoth, lxml |
