# Doc Translation Review Platform

A compliance-grade, end-to-end pipeline + reviewer app for translating
clinical/regulated `.docx` documents on Databricks.

A submitter drops a Japanese (or any source-language) `.docx` into a Unity
Catalog Volume. A file-arrival-triggered Lakeflow job translates it in-place
at the OOXML level with Foundation Model API. Reviewers certify it
paragraph-by-paragraph in a React app (FastAPI backend). Once 100% certified,
the document is atomically promoted to a "golden" Volume location, locked
read-only, and mirrored to Delta for long-term archive.

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

**Glossary mining + injection (the feedback loop)** — the `translation_glossary` table holds terminology entries of three kinds, distinguished by a `source` column:
- `tenant` — mined by scanning `review_edit_history` for repeated (model output → reviewer correction) patterns across documents and reviewers.
- `seed` — optional public clinical terminology shipped with the app (`setup/seed_glossary/*.csv`, ~115 ICH/GCP JA→EN terms). Loaded at postdeploy when `enable_seed_glossary=true`.
- `customer` — bilingual pairs the customer imports via CSV (Glossary tab → Import, or drop a CSV in the `glossary_imports/` Volume folder).

Approved entries are mirrored to a Delta table and read by the translation pipeline at startup, which builds an **Aho-Corasick automaton** over the source-language phrases. For each paragraph the pipeline injects only the glossary entries whose term actually appears in that paragraph into the FMAPI system prompt — so per-call prompt cost is a function of the segment, not the total glossary size, and the design scales to large enterprise glossaries. The result: a correction made once by a reviewer is applied automatically to every future translation of that term.

**Language handling** — the source language is auto-detected per document (`langdetect`) and recorded in `bronze_documents.source_language`; the target language is a selectable setting (`translation_target_language` bundle var, overridable per-run from the Jobs UI). No language pair is hard-coded.

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
| Storage (files) | Original / translated / reviewed / golden `.docx` | UC Volume `<uc_catalog>.<uc_schema>.<uc_volume_name>` (from your `variable-overrides.json`) |
| Storage (state) | Live review state — pairs, feedback, edits, audit, glossary, confidence | Lakebase Postgres (Project or Provisioned), schema `<pg_schema>` |
| Storage (archive) | Long-term audit + publication archive | Delta tables in `<uc_catalog>.<uc_schema>` |
| Translation | In-place OOXML translation per paragraph | FMAPI endpoint (default `databricks-claude-sonnet-4-6`, configurable via `translation_model_endpoint`) |
| Orchestration | File-arrival → translation pipeline | Lakeflow Job `doc-translation · auto-translate pipeline` (bundle-managed) |
| Review UI | Two-region layout (side-by-side DOCX HTML preview + paragraph action rail), certify/edit/publish/promote, glossary admin, audit | React SPA (Vite + TS + Tailwind) served by FastAPI on Databricks Apps |
| Auth | Reviewer identity via `X-Forwarded-Email`, App SP for system actions | Databricks Apps SSO + Service Principal |

---

## Repository layout

```
doc-translation-app/
├── databricks.yml                  # DAB bundle root
├── variables.yml                   # Bundle variable definitions
├── variable-overrides.example.json # Template customer fills in per workspace
├── deploy.sh                       # deploy wrapper (build → seed → deploy → postdeploy → app)
├── build.sh                        # builds the React frontend → static/ (run before deploy)
├── server_api.py                   # FastAPI backend: JSON review API + serves the SPA
├── app.yaml                        # Databricks Apps runtime config (uvicorn server_api:app)
├── requirements.txt                # fastapi, uvicorn, psycopg[binary,pool], databricks-sdk, mammoth, lxml
├── pyproject.toml
├── frontend/                       # React SPA source (Vite + TS + Tailwind); NOT deployed
│   ├── package.json
│   ├── vite.config.ts              # builds to ../static/
│   └── src/
│       ├── App.tsx                 # tab shell: Review / Glossary / Audit
│       ├── api.ts                  # typed client for /api/*
│       └── components/
│           ├── review/             # ReviewView, PreviewPane, ParagraphCard
│           ├── glossary/GlossaryView.tsx
│           └── audit/AuditView.tsx
├── static/                        # PREBUILT SPA (committed; served by FastAPI)
├── legacy/
│   └── streamlit_app.py            # previous Streamlit UI, kept for reference/rollback
├── resources/                      # DAB-managed resources
│   ├── app.yml                     # App definition + postgres/sql_warehouse/secret bindings
│   ├── schema.yml                  # UC schema
│   ├── volumes.yml                 # UC managed volume
│   └── jobs/
│       ├── postdeploy_setup.yml    # One-shot job: DDL + GRANTs + Delta tables + secret seed
│       └── translation_pipeline.yml # File-arrival-triggered translation job
├── setup/                          # Notebooks bundled into the workspace
│   ├── postdeploy.py               # Postdeploy notebook (idempotent)
│   ├── auto_translate_watcher.py   # Watcher: scans raw_documents/, calls translator per file
│   ├── docx_inplace_translation.py # Per-file translator (FMAPI + lxml + glossary injection)
│   └── seed_glossary/              # Optional public clinical seed CSVs (source='seed')
├── server/
│   ├── auth.py                     # Reviewer from X-Forwarded-Email
│   ├── config.py                   # env + WorkspaceClient singleton (Project + Provisioned)
│   ├── confidence.py               # Heuristic per-paragraph scoring (no LLM)
│   ├── db.py                       # Lakebase psycopg pool, lazy-init proxy
│   ├── delta_sync.py               # Lakebase → Delta mirror (review state + glossary)
│   ├── docx_render.py              # DOCX → HTML via sentinel markers + lxml
│   ├── glossary.py                 # Mine + ingest + prompt-format glossary entries
│   ├── store.py                    # Lakebase CRUD + audit emits + lifecycle
│   ├── styles.py                   # (legacy Streamlit CSS)
│   └── volume.py                   # UC Volume listing, DOCX read, golden promotion
└── docs/
    ├── architecture.png            # Architecture diagram (PNG)
    ├── architecture.svg            # Architecture diagram (SVG)
    └── pipeline_design.md          # Phase 1+ design doc
```

---

## Deploying to your own workspace (Databricks Asset Bundle)

This branch is set up as a portable Databricks Asset Bundle (DAB). One
`./deploy.sh` from a clean clone, after a one-time config file edit.

### Prerequisites

Your target workspace needs:

1. **Unity Catalog access** — a catalog where you can `CREATE SCHEMA` and `CREATE VOLUME`
2. **Lakebase** — either:
   - A **Lakebase Project** (Autoscaling) — preferred, the new way. Get the project name + branch.
   - …or a legacy **Provisioned** instance. Get the instance name.
3. **A SQL warehouse** for the Delta archive. Any serverless 2X-Small works.
4. **Foundation Model API** with access to `databricks-claude-sonnet-4-6` (or another Claude model you specify in the inner translation notebook).
5. **Databricks CLI** v0.220+ authenticated to the target workspace.

### One-time setup

```bash
# 1. Clone + check out this branch
git clone git@github.com:guanyudb/doc-translation.git
cd doc-translation
git checkout customer-deployable

# 2. Create the variable-overrides file. Its presence is what enables the
#    "Deploy bundle" button in the Workspace UI AND what `./deploy.sh` reads.
mkdir -p .databricks/bundle/prod
cp variable-overrides.example.json .databricks/bundle/prod/variable-overrides.json
# Edit .databricks/bundle/prod/variable-overrides.json:
#   - workspace_user_email:    your-email@org.com  (deploying user + Lakebase admin)
#   - uc_catalog:              <catalog you can CREATE SCHEMA on>
#   - lakebase_project:        <your Lakebase Project name>      [Project mode]
#   - lakebase_branch:         production                        [or main — check your Project]
#   - lakebase_database_slug:  databricks-postgres               [usually]
#   - lakebase_instance:       ""                                [empty if using Project]
#   - warehouse_id:            <your SQL warehouse ID>
#   - pg_schema:               doc_translation                   [Postgres schema name]
```

> **The workspace host comes from your Databricks CLI profile**, not this file. Bundle variables can't be referenced from `workspace.host` (auth resolves before variable substitution). Either run with `--profile <name>` or set `DATABRICKS_HOST` in your environment.

If you're on a **legacy Provisioned** Lakebase instance instead of a Project:
- Set `lakebase_instance` to your instance name; leave `lakebase_project` empty.
- In `resources/app.yml`, comment out the `postgres:` binding block and uncomment the `database:` block underneath it.

### Deploy (CLI — one command, recommended)

```bash
./deploy.sh
```

This runs the 4-step deploy in order:

1. **Seed secrets** — reads `variable-overrides.json`, creates the scope, puts 7 secrets (`pg_schema`, `lakebase_project`, `lakebase_branch`, `lakebase_instance`, `volume_root`, `delta_catalog`, `delta_schema`). Idempotent.
2. `bundle deploy` — creates UC schema + volume + app (with secret + postgres + warehouse bindings) + postdeploy job, syncs code to the workspace.
3. `bundle run postdeploy_setup` — Lakebase DDL, GRANTs the App SP `USAGE + CREATE on public` and table/sequence perms, creates the Delta mirror tables, pre-creates Volume subdirectories.
4. `bundle run doc_translation_app` — pushes the source from the bundle's workspace files into the App runtime and starts it.

The app URL prints at the end. First boot takes ~30 seconds.

> **⚠ Always re-run postdeploy after any `bundle deploy`.** The file-arrival
> trigger on the translation pipeline is attached by **postdeploy** (via the
> Jobs API), not by the bundle YAML — `resources/jobs/translation_pipeline.yml`
> deliberately omits it to avoid a create-time race against the Volume. But
> every `bundle deploy` re-applies the job from that YAML and **wipes the
> trigger**. `./deploy.sh` always runs postdeploy (step 3) so the full flow is
> safe. If you ever run `bundle deploy` on its own (e.g. a quick code push),
> follow it with `bundle run -t <target> doc_translation_postdeploy_setup` or
> uploads will land in `raw_documents/` with nothing listening. Symptom: files
> appear in the Upload dialog's status list stuck on **queued** and never
> start translating.

### Deploy (Workspace UI button)

The "Deploy bundle" button **alone is not enough.** Apps validates secret resource bindings eagerly at app create/update time, so the bundle deploy will 404 unless the secret values already exist.

To use the UI:

1. **One-time, via CLI**: run `./deploy.sh` once. This seeds the secrets + does the full deploy.
2. **Subsequent code changes**: now the UI Deploy bundle button works fine (secrets already exist; the bundle just updates the app). After clicking it, also: Workflows → `doc-translation · postdeploy setup` → **Run now** (if your schema/DDL changed), then Apps → your app → **Deploy** (to push the source change to the runtime).

Or always run `./deploy.sh` for iteration — it's idempotent and handles all 4 steps in one command.

If you ever change `variable-overrides.json` values (e.g., point at a different Lakebase Project), re-run `./deploy.sh` so the seeded secrets are updated.

### Translation pipeline

The bundle deploys **both** the reviewer app AND the auto-translation pipeline.

After `./deploy.sh`, you'll have a Lakeflow job called `doc-translation · auto-translate pipeline` with:
- **A file-arrival trigger** watching `/Volumes/<your-catalog>/<your-schema>/<your-volume>/raw_documents/`. Fires 60s after the last upload (debounce, so a batch upload kicks one run not N).
- **Two notebooks** that ship with the bundle:
  - `setup/auto_translate_watcher.py` — scans for unpaired files, writes `bronze_documents` audit rows, invokes the translator once per file
  - `setup/docx_inplace_translation.py` — translates paragraph-by-paragraph via the configured Foundation Model API endpoint, in-place at the OOXML level (preserves layout, charts, headers/footers, SmartArt)

To kick a translation: just upload a `.docx` to `raw_documents/`. The job fires automatically; the translated file appears in `translated_inplace/`; the reviewer app's sidebar picks up the new pair.

Configuration knobs (set in `variable-overrides.json`):
- `translation_model_endpoint` (default `databricks-claude-sonnet-4-6`)
- `translation_target_language` (default `English`)
- `translation_max_workers` (default `8`)
- `translation_max_pages` (default `0` = whole document)

### Re-deploys

`./deploy.sh` is idempotent. Run it again after code changes; it picks up the diff. Schema migrations in the postdeploy job use `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD COLUMN IF NOT EXISTS` so re-running is safe.

**Self-healing pattern:** the postdeploy job re-seeds all secret values from its own bundle-variable parameters (no shell-quoting hazards, unlike `deploy.sh`'s step 1). So if `deploy.sh` ever produces wrong secret values (a past bug shifted fields when `lakebase_instance=""`), re-running just the postdeploy job from the UI fixes them — no CLI required.

### Tearing down

`bundle destroy` works when the local terraform state matches the remote — which it doesn't if someone else (or another machine) ran `bundle deploy` since you last did. If you hit `Error: lineage mismatch in state files`, fall back to deleting resources directly:

```bash
# Drop Delta tables first (postdeploy created them, bundle doesn't track them,
# and the schema delete below fails if it's non-empty).
for t in audit_events bronze_documents golden_publications silver_review_snapshots; do
  databricks api post /api/2.0/sql/statements --profile <profile> \
    --json "{\"statement\":\"DROP TABLE IF EXISTS <catalog>.<schema>.${t}\",\"warehouse_id\":\"<warehouse>\",\"wait_timeout\":\"30s\"}"
done

# Now delete the actual resources
databricks apps delete    doc-translation                                            --profile <profile>
databricks jobs delete    <pipeline-job-id>                                          --profile <profile>
databricks jobs delete    <postdeploy-job-id>                                        --profile <profile>
databricks volumes delete <catalog>.<schema>.<volume>                                --profile <profile>
databricks schemas delete <catalog>.<schema>                                         --profile <profile>
databricks secrets delete-scope doc_translation_config                               --profile <profile>
databricks workspace delete /Workspace/Users/<you>/.bundle/doc-translation --recursive --profile <profile>
```

The Lakebase Project itself stays (other things may use it); to also drop the Postgres schema inside it, run `DROP SCHEMA <pg_schema> CASCADE` against the project's primary endpoint.

### Troubleshooting cross-references

The hardest-to-find issues during initial customer deploys, in the order we hit them:

| Symptom | Cause | Fix |
|---|---|---|
| `Invalid secret resource pg_schema: Secret … does not exist` (bundle deploy 404) | Apps validates secret bindings eagerly at app create/update — secrets must exist before the bundle deploy | `deploy.sh` step 1 seeds secrets first; CLI is required for the very first deploy |
| App boots, sidebar empty, log says `failed to resolve host 'None'` or `${var.lakebase_project}` literal | DAB does NOT substitute `${var.X}` inside `app.yaml` — values must come via `valueFrom:` to secret bindings | All env vars use `valueFrom:`; `deploy.sh` + postdeploy seed the secret values |
| `Endpoint 'projects/<proj>/branches/<br>/endpoints/primary' not found` | Lakebase Project's default branch is `production` (not `main`) on new Projects | Set `lakebase_branch: production` in `variable-overrides.json` |
| Inner translator crashes with `AttributeError: 'ServingEndpointsAPI' object has no attribute 'get_open_ai_client'` | Workspace default serverless env is v1 with an old `databricks-sdk` | `resources/jobs/translation_pipeline.yml` pins `client: "5"` so the watcher + inner notebook get a modern SDK |
| Sidebar warning "Couldn't read some Volume paths" + listing 404s with malformed path | Secret values shifted by one slot due to a shell-quoting bug | Re-run the postdeploy job — it re-seeds secrets defensively |
| App SP can't list Volume even though grants look right | `USE CATALOG`/`USE SCHEMA` granted but `READ VOLUME` missing | postdeploy job now grants `READ VOLUME, WRITE VOLUME` explicitly |
| `pool has already been opened/closed and cannot be reused` | psycopg-pool 3.2+ is strict; multi-session Apps races on the module-level pool | `server/db.py` proxies a lazy-built pool that rebuilds on `closed` |

More general DAB+Apps gotchas live in [`~/.claude/memory/dab_apps_workspace_deploy_guide.md`](../../.claude/memory/dab_apps_workspace_deploy_guide.md).

### Legacy: deploying just the app (no bundle)

For a quick code-only deploy when the workspace is already configured:

```bash
databricks sync . /Workspace/Users/<you>/databricks_apps/doc-translation \
  --full --exclude __pycache__ --exclude .gitignore --exclude .venv \
  --profile <profile>

databricks apps deploy doc-translation \
  --source-code-path /Workspace/Users/<you>/databricks_apps/doc-translation \
  --no-wait --profile <profile>
```

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

# Terminal 1 — FastAPI backend
DATABRICKS_PROFILE=<profile> \
PGHOST=instance-...database.cloud.databricks.com \
PGPORT=5432 PGDATABASE=databricks_postgres PGSSLMODE=require \
PGSCHEMA=doc_translation LAKEBASE_INSTANCE=lakebasepoc \
VOLUME_ROOT=/Volumes/.../doc-translation \
DATABRICKS_WAREHOUSE_ID=<warehouse-id> \
DELTA_CATALOG=hls_amer_catalog DELTA_SCHEMA=guanyu_chen \
.venv/bin/uvicorn server_api:app --port 8000 --reload

# Terminal 2 — Vite dev server (proxies /api → localhost:8000)
cd frontend && npm install && npm run dev   # http://localhost:5173
```

For a production-shaped local run, `./build.sh` then hit the FastAPI port directly
(it serves the built SPA from `static/`). The legacy Streamlit UI is still runnable
with `.venv/bin/streamlit run legacy/streamlit_app.py` against the same env vars.

---

## What's deferred

- **Paragraph/sentence-level correction learning** — semantic retrieval (Databricks Vector Search) over past (original, revised) paragraph pairs, injected as few-shot examples. Complements the term-level glossary; gated on classifying reviewer feedback as term- vs sentence-level first.
- **PDF support** — designed but not built; would need `pymupdf` extraction + an "overlay vs. in-place" writeback choice per document
- **Two-eyes / multi-reviewer attestation** before promotion
- **AI/BI dashboards** on the Delta tables (pipeline health, review backlog, SLA breach)
- **Lakeflow alerts** on `FAILED_TRANSLATION` / `DELTA_SYNC_FAILED` events
