# Doc Translation Review Platform

A compliance-grade platform for translating and reviewing clinical/regulated
**`.docx` and `.pdf`** documents on Databricks — deployed inside your own
workspace and Unity Catalog.

A submitter uploads a source-language document; it's translated automatically;
reviewers certify it paragraph-by-paragraph in a web app; and once fully
certified it's promoted to an immutable "golden" copy with a complete audit
trail. It runs entirely on Databricks primitives — Unity Catalog Volumes,
Lakebase Postgres, Foundation Model API / AI Gateway, Lakeflow, and Databricks
Apps.

## What it does

- **Translate DOCX and PDF through one review experience.** DOCX is translated
  in place at the document level (layout, tables, headers/footers preserved);
  PDF is parsed, translated, and can be re-exported as a **layout-preserving
  translated PDF**. Reviewers can't tell the two apart.
- **Review paragraph-by-paragraph** — edit the translation, certify, flag, or
  comment, with page-level and whole-document bulk certify. A heuristic
  confidence score highlights paragraphs worth a closer look.
- **Re-translate in place** with a different instruction — no re-upload.
- **Download** the translated document at any point, with the current edits applied.
- **Glossary** of approved terminology as managed named lists (batch
  enable/disable, conflict detection, CSV import). A correction a reviewer makes
  once is applied automatically to future translations of that term.
- **Instructions** — a library of named translation prompts; one is chosen (and
  frozen) per document, so editing a prompt never changes what a past document
  was translated with.
- **Choose the model** — any Databricks chat serving endpoint or a Unity Catalog
  **AI Gateway** endpoint, selected from Settings.
- **Brand it** — set the app title and logo from Settings (the logo can be an
  `https://` URL, a `data:` URI, or a UC Volume path the app serves).
- **Admin controls** — only the deploying user (or a configured admin list) can
  change Settings or **permanently remove a document**; everyone else reviews.
  Promoted/published documents are protected from deletion.
- **Compliance built in** — every action is audited (append-only, 7-year
  retention), every certified document is content-addressed (SHA-256), and
  promotion locks the document read-only and mirrors it to Delta for archive.

## How it works

```
[Upload .docx / .pdf]
      │
      ├─ DOCX → file-arrival Lakeflow job → in-place OOXML translation
      └─ PDF  → in-app parse (ai_parse_document) + element translation
      │
      ▼
[ Review app on Databricks Apps ]  ── live state in Lakebase Postgres
   certify · edit · comment · re-translate · download
      │
      ▼  (100% certified, no flags)
[ Golden Volume ]  SHA-256, read-only  ──►  [ Delta archive ]  audit + publications
```

Two ingestion paths converge on one format-agnostic review UI. Two storage
planes keep the hot path fast and the cold path compliance-grade: **Lakebase
Postgres** holds live review state; **Delta Lake** is the append-only archive,
written only at promotion time.

Documents move through `UNDER_REVIEW → PROMOTING → PUBLISHED → ARCHIVED`. Once
`PUBLISHED`, writes are refused and the attempt is itself audited — so a full
history for any document is one SQL query against the Delta tables.

## Deploy it to your workspace

The repo is a portable Databricks Asset Bundle — one `./deploy.sh` from a clean
clone after a one-time config edit.

**Prerequisites (target workspace):**
- A **Unity Catalog** you can `CREATE SCHEMA` + `CREATE VOLUME` in.
- A **Lakebase Autoscaling Project** (note its name + branch, usually `production`).
- A **Serverless or Pro SQL warehouse** on **DBR 17.3+** (the PDF workflow uses `ai_parse_document`).
- **Foundation Model API** access to a Claude (or other chat) endpoint.
- Locally: **Databricks CLI v1.15+**, **Node.js**, **git**, and a CLI profile authenticated to the workspace.

**Steps:**

```bash
# 1. Clone
git clone <repo-url> && cd doc-translation-app && git checkout main

# 2. Discover your workspace values
databricks postgres list-projects  --profile <p>
databricks postgres list-branches  projects/<project> --profile <p>
databricks postgres list-databases projects/<project>/branches/<branch> --profile <p>
databricks warehouses list         --profile <p>

# 3. Create the per-workspace config, then fill it in
./init.sh prod   # creates .databricks/bundle/prod/variable-overrides.json from the template
#   edit: workspace_user_email, uc_catalog, lakebase_project, lakebase_branch,
#         lakebase_database_slug, warehouse_id, app_name, (optional) app_admin_emails

# 4. Deploy
./deploy.sh prod --profile <p>
#   Terraform GPG "key expired"? Run this once and re-run:
#   export DATABRICKS_TF_EXEC_PATH=$(which terraform) DATABRICKS_TF_VERSION=1.5.7
```

`deploy.sh` builds the app, seeds config, deploys the bundle (UC schema + volume
+ app + jobs), runs one-time setup (schema, grants, file-arrival trigger, Delta
tables), and starts the app. The URL prints at the end. It's idempotent — re-run
it after any change (re-deploys require CLI v1.15+).

**First run:** open the URL (Databricks SSO) and complete **Settings** — model
endpoint and default target language. Only the deploying user (or anyone in
`app_admin_emails`) can change Settings; everyone else reviews.

**Smoke test:** upload a `.docx` and a `.pdf`. Each appears in the review list
once translated (DOCX ~1–3 min via the job; PDF ~10–60 s in-app). Certify a few
paragraphs and **Download** the result.

> The workspace host comes from your CLI profile, not the config file — always
> pass `--profile <p>` (or set `DATABRICKS_HOST`).

## Using the app

**Reviewers** pick a document from the dropdown, read the side-by-side panes
(source left, translation right), and for each paragraph edit / certify / flag /
comment — or bulk-certify a page or the whole document. If there are edits,
**Publish** bakes a versioned reviewed copy. When everything is certified with
no flags, **Promote to Gold** writes the immutable golden copy and locks the
document. The **Audit** tab shows the full history any time.

**Admins** (the deployer, plus anyone in `app_admin_emails`) additionally can:

- Change **Settings** — model endpoint, default language, and branding (title + logo).
- **Re-translate** a document with a different instruction (resets its review state).
- **Delete** a document — permanently removes its source, translation, and
  review state so it drops off the list. Promoted/published documents are
  protected; the deletion is recorded in the audit trail and the compliance
  archive is preserved.

## Configuration

Per-workspace values live in `.databricks/bundle/<target>/variable-overrides.json`
(created by `./init.sh`, gitignored). Runtime settings are managed in-app from
**Settings** and change without a redeploy.

| Setting | Where | Notes |
|---|---|---|
| Model endpoint | Settings | Any chat serving endpoint, or a UC AI Gateway endpoint (3-part `catalog.schema.name`) — grant the app's service principal access to it. |
| Default target language | Settings | Pre-selected at upload; each upload can override. Source language is auto-detected. |
| Branding (title, logo) | Settings | Logo = `https://` URL, `data:` URI, or a `/Volumes/…` path the app serves. |
| Admins | `app_admin_emails` (config) | Comma-separated; blank = only the deploying user. |
| Seed glossary | `enable_seed_glossary` (config) | Loads ~115 ICH/GCP clinical terms on first deploy. |

## Operations

- **Re-deploy / update:** re-run `./deploy.sh <target> --profile <p>`. Idempotent;
  schema changes (`server/schema.sql`) apply automatically.
- **Teardown:** `databricks bundle destroy -t <target> --profile <p>`, then drop
  the Delta tables and the Postgres schema separately (the bundle doesn't track them).

**Common issues:**

| Symptom | Fix |
|---|---|
| `Invalid update mask` when re-deploying | Upgrade the Databricks CLI to v1.15+ (first deploys are unaffected). |
| Uploads stuck on "Queued", never translate | Re-run the postdeploy step — a bare `bundle deploy` resets the file-arrival trigger; `./deploy.sh` always re-attaches it. |
| PDF upload never finishes | Warehouse must be Serverless/Pro on DBR 17.3+ (needs `ai_parse_document`). |
| Terraform GPG "key expired" during deploy | `export DATABRICKS_TF_EXEC_PATH=$(which terraform) DATABRICKS_TF_VERSION=1.5.7`, then re-run. |
| Lakebase endpoint "not found" | Set `lakebase_branch` to your project's actual branch (new projects default to `production`, not `main`). |

---

Everything runs inside your Databricks workspace and Unity Catalog, governed by
your own permissions and service principal.
