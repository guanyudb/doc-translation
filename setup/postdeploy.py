# Databricks notebook source
# MAGIC %md
# MAGIC # Postdeploy setup — doc-translation
# MAGIC
# MAGIC One-shot job, idempotent. Runs as the deploying workspace user (who
# MAGIC owns the Lakebase database + UC catalog/schema/volume).
# MAGIC
# MAGIC Steps:
# MAGIC 1. Resolve the App service principal's UUID (from the deployed app).
# MAGIC 2. Mint a Lakebase OAuth JWT for the deployer (NOT a PAT).
# MAGIC 3. Connect to the Lakebase database and:
# MAGIC    - run `store.ensure_schema()` (idempotent)
# MAGIC    - GRANT USAGE + CREATE on schema `public` to the App SP
# MAGIC    - GRANT SELECT/INSERT/UPDATE/DELETE on the doc_translation tables
# MAGIC    - GRANT USAGE on sequences (BIGSERIAL columns)
# MAGIC    - set default privileges so future tables auto-grant
# MAGIC 4. Create the Delta mirror tables via the SQL warehouse (the App SP
# MAGIC    also needs SELECT/MODIFY on these — granted in step 5).
# MAGIC 5. Pre-create the Volume subdirectories the app expects.
# MAGIC
# MAGIC Re-run anytime. All operations are MERGE / IF NOT EXISTS / EXCLUDED-aware.

# COMMAND ----------

# MAGIC %pip install psycopg[binary,pool] databricks-sdk lxml --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
import sys
import json
import uuid

dbutils.widgets.text("uc_catalog",             "", "uc_catalog")
dbutils.widgets.text("uc_schema",              "doc_translation", "uc_schema")
dbutils.widgets.text("uc_volume_name",         "doc-translation", "uc_volume_name")
dbutils.widgets.text("pg_schema",              "doc_translation", "pg_schema")
dbutils.widgets.text("lakebase_project",       "", "lakebase_project (empty = use Provisioned)")
dbutils.widgets.text("lakebase_branch",        "main", "lakebase_branch")
dbutils.widgets.text("lakebase_database_slug", "databricks-postgres", "lakebase_database_slug")
dbutils.widgets.text("lakebase_instance",      "", "lakebase_instance (Provisioned fallback)")
dbutils.widgets.text("warehouse_id",           "", "warehouse_id")
dbutils.widgets.text("app_name",               "doc-translation", "app_name")
dbutils.widgets.text("secret_scope",           "doc_translation_config", "secret_scope")

uc_catalog        = dbutils.widgets.get("uc_catalog").strip()
uc_schema         = dbutils.widgets.get("uc_schema").strip()
uc_volume_name    = dbutils.widgets.get("uc_volume_name").strip()
pg_schema         = dbutils.widgets.get("pg_schema").strip() or "doc_translation"
lakebase_project  = dbutils.widgets.get("lakebase_project").strip()
lakebase_branch   = dbutils.widgets.get("lakebase_branch").strip() or "main"
lakebase_db_slug  = dbutils.widgets.get("lakebase_database_slug").strip()
lakebase_instance = dbutils.widgets.get("lakebase_instance").strip()
warehouse_id      = dbutils.widgets.get("warehouse_id").strip()
app_name          = dbutils.widgets.get("app_name").strip()
secret_scope      = dbutils.widgets.get("secret_scope").strip() or "doc_translation_config"

for k, v in [("uc_catalog", uc_catalog), ("warehouse_id", warehouse_id), ("app_name", app_name)]:
    if not v:
        raise SystemExit(f"missing required parameter: {k}")
if not lakebase_project and not lakebase_instance:
    raise SystemExit("either lakebase_project (Project mode) or lakebase_instance (Provisioned mode) must be set")
print(f"catalog={uc_catalog}.{uc_schema}.{uc_volume_name}")
print(f"lakebase mode: {'Project' if lakebase_project else 'Provisioned'}")
print(f"warehouse_id={warehouse_id}")
print(f"app_name={app_name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Resolve the App service principal UUID
# MAGIC
# MAGIC Note: secret values are seeded by `deploy.sh` (step 1) BEFORE `bundle
# MAGIC deploy`, because Apps validates secret resource bindings eagerly at
# MAGIC app update time. By the time this notebook runs, the secrets already
# MAGIC exist; we just need to do the Lakebase DDL + GRANTs and create the
# MAGIC Delta tables + Volume dirs.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

# Apps's SP is created automatically and accessible via the apps API.
app = w.apps.get(name=app_name)
sp_uuid = app.service_principal_client_id
if not sp_uuid:
    raise SystemExit(f"could not resolve service_principal_client_id for app {app_name!r}")
print(f"app SP UUID: {sp_uuid}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Mint a Lakebase OAuth JWT for the deployer (NOT a PAT)

# COMMAND ----------

if lakebase_project:
    # Lakebase Project mode — POST /api/2.0/postgres/credentials
    endpoint = f"projects/{lakebase_project}/branches/{lakebase_branch}/endpoints/primary"
    resp = w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": endpoint})
    pg_token = resp["token"]
    # Discover host. The endpoint metadata returns it nested under
    # status.hosts.host (the read/write endpoint). The pooled variant is
    # status.hosts.read_write_pooled_host — fine to use that too for
    # postdeploy DDL but the direct host is simpler.
    epresp = w.api_client.do("GET", f"/api/2.0/postgres/{endpoint}")
    hosts = (epresp.get("status") or {}).get("hosts") or {}
    pg_host = hosts.get("host") or hosts.get("read_write_pooled_host")
    if not pg_host:
        raise SystemExit(
            f"could not resolve Lakebase host from endpoint metadata.\n"
            f"GET /api/2.0/postgres/{endpoint} returned: {epresp}"
        )
    pg_db   = "databricks_postgres"  # Lakebase Projects default
else:
    # Legacy Provisioned mode
    cred = w.database.generate_database_credential(instance_names=[lakebase_instance])
    pg_token = cred.token
    inst = w.database.get_database_instance(name=lakebase_instance)
    pg_host = inst.read_write_dns
    pg_db   = "databricks_postgres"

print(f"pg_host: {pg_host}")
print(f"pg_db:   {pg_db}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Schema migration + GRANTs

# COMMAND ----------

import psycopg

pg_user = w.current_user.me().user_name
conninfo = f"dbname={pg_db} user={pg_user} host={pg_host} port=5432 sslmode=require"

# We can't import server/store.py here (different runtime); inline the DDL.
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {pg_schema};

CREATE TABLE IF NOT EXISTS {pg_schema}.review_pairs (
    pair_id          TEXT PRIMARY KEY,
    original_path    TEXT NOT NULL,
    translated_path  TEXT NOT NULL,
    source_lang      TEXT,
    target_lang      TEXT,
    total_paragraphs INT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    finalized_at     TIMESTAMPTZ,
    lifecycle_state  TEXT NOT NULL DEFAULT 'UNDER_REVIEW'
                      CHECK (lifecycle_state IN ('UNDER_REVIEW','PROMOTING','PUBLISHED','ARCHIVED')),
    locked_at        TIMESTAMPTZ,
    original_hash    TEXT,
    translated_hash  TEXT
);

CREATE TABLE IF NOT EXISTS {pg_schema}.review_feedback (
    pair_id        TEXT REFERENCES {pg_schema}.review_pairs(pair_id) ON DELETE CASCADE,
    paragraph_idx  INT  NOT NULL,
    status         TEXT DEFAULT 'pending' CHECK (status IN ('pending','certified','flagged')),
    comment        TEXT,
    reviewer       TEXT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    edited_text    TEXT,
    last_published_text TEXT,
    PRIMARY KEY (pair_id, paragraph_idx)
);
CREATE INDEX IF NOT EXISTS review_feedback_status_idx ON {pg_schema}.review_feedback (pair_id, status);

CREATE TABLE IF NOT EXISTS {pg_schema}.review_edit_history (
    edit_id        BIGSERIAL PRIMARY KEY,
    pair_id        TEXT NOT NULL REFERENCES {pg_schema}.review_pairs(pair_id) ON DELETE CASCADE,
    paragraph_idx  INT  NOT NULL,
    previous_text  TEXT,
    new_text       TEXT,
    reviewer       TEXT,
    edited_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_edit_history_pair_idx
ON {pg_schema}.review_edit_history (pair_id, paragraph_idx, edited_at DESC);

CREATE TABLE IF NOT EXISTS {pg_schema}.review_publish_log (
    publish_id      BIGSERIAL PRIMARY KEY,
    pair_id         TEXT NOT NULL REFERENCES {pg_schema}.review_pairs(pair_id) ON DELETE CASCADE,
    output_path     TEXT NOT NULL,
    edits_applied   INT  NOT NULL DEFAULT 0,
    published_by    TEXT,
    published_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {pg_schema}.audit_events (
    event_id        BIGSERIAL PRIMARY KEY,
    pair_id         TEXT,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    actor_type      TEXT NOT NULL DEFAULT 'human',
    paragraph_idx   INT,
    before_value    JSONB,
    after_value     JSONB,
    correlation_id  TEXT,
    client_ip       TEXT,
    client_ua       TEXT,
    event_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_events_pair_idx ON {pg_schema}.audit_events (pair_id, event_at DESC);

CREATE TABLE IF NOT EXISTS {pg_schema}.golden_publications (
    publication_id          BIGSERIAL PRIMARY KEY,
    pair_id                 TEXT NOT NULL UNIQUE REFERENCES {pg_schema}.review_pairs(pair_id),
    golden_original_path    TEXT NOT NULL,
    golden_translated_path  TEXT NOT NULL,
    golden_original_hash    TEXT NOT NULL,
    golden_translated_hash  TEXT NOT NULL,
    total_paragraphs        INT  NOT NULL,
    certified_paragraphs    INT  NOT NULL,
    edits_applied           INT  NOT NULL DEFAULT 0,
    distinct_reviewers      JSONB NOT NULL DEFAULT '[]'::jsonb,
    published_by            TEXT NOT NULL,
    published_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    delta_synced_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS {pg_schema}.paragraph_confidence (
    pair_id          TEXT NOT NULL REFERENCES {pg_schema}.review_pairs(pair_id) ON DELETE CASCADE,
    paragraph_idx    INT  NOT NULL,
    length_ratio     REAL,
    untranslated_pct REAL,
    repeated_ngrams  INT,
    confidence       REAL NOT NULL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_lang      TEXT,
    target_lang      TEXT,
    PRIMARY KEY (pair_id, paragraph_idx)
);

CREATE TABLE IF NOT EXISTS {pg_schema}.translation_glossary (
    entry_id            BIGSERIAL PRIMARY KEY,
    source_lang         TEXT,
    target_lang         TEXT,
    model_phrase        TEXT NOT NULL,
    correction          TEXT NOT NULL,
    occurrences         INT  NOT NULL DEFAULT 1,
    distinct_reviewers  INT  NOT NULL DEFAULT 1,
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved            BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (source_lang, target_lang, model_phrase, correction)
);
"""

GRANTS_SQL = f"""
GRANT USAGE, CREATE ON SCHEMA public TO "{sp_uuid}";
GRANT USAGE ON SCHEMA {pg_schema} TO "{sp_uuid}";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {pg_schema} TO "{sp_uuid}";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {pg_schema} TO "{sp_uuid}";
ALTER DEFAULT PRIVILEGES IN SCHEMA {pg_schema}
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{sp_uuid}";
ALTER DEFAULT PRIVILEGES IN SCHEMA {pg_schema}
  GRANT USAGE, SELECT ON SEQUENCES TO "{sp_uuid}";
GRANT UPDATE ON {pg_schema}.review_pairs TO "{sp_uuid}";
"""

with psycopg.connect(conninfo, password=pg_token) as conn:
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(GRANTS_SQL)
    conn.commit()
print("ok: Lakebase schema + grants applied")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Delta mirror tables + grants

# COMMAND ----------

DELTA_FQN = f"{uc_catalog}.{uc_schema}"

def _exec(stmt):
    """Run one statement against the configured SQL warehouse."""
    from databricks.sdk.service.sql import StatementState
    import time as _t
    r = w.statement_execution.execute_statement(
        statement=stmt, warehouse_id=warehouse_id, wait_timeout="30s",
    )
    deadline = _t.monotonic() + 120
    while True:
        s = r.status.state if r.status else None
        if s == StatementState.SUCCEEDED:
            return
        if s in (StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED):
            err = r.status.error.message if (r.status and r.status.error) else "(no message)"
            raise RuntimeError(f"Delta stmt {s}: {err}\nSQL: {stmt[:300]}")
        if _t.monotonic() > deadline:
            raise RuntimeError(f"Delta stmt timed out")
        _t.sleep(0.4)
        r = w.statement_execution.get_statement(r.statement_id)

_exec(f"CREATE SCHEMA IF NOT EXISTS {DELTA_FQN}")
_exec(f"""
    CREATE TABLE IF NOT EXISTS {DELTA_FQN}.audit_events (
        event_id BIGINT, pair_id STRING, event_type STRING, actor STRING,
        actor_type STRING, paragraph_idx INT,
        before_value STRING, after_value STRING,
        correlation_id STRING, client_ip STRING, client_ua STRING,
        event_at TIMESTAMP, ingested_at TIMESTAMP
    ) USING DELTA
    TBLPROPERTIES (
        delta.appendOnly = 'true',
        delta.deletedFileRetentionDuration = 'interval 2557 days',
        delta.logRetentionDuration         = 'interval 2557 days'
    )
""")
_exec(f"""
    CREATE TABLE IF NOT EXISTS {DELTA_FQN}.golden_publications (
        publication_id BIGINT, pair_id STRING,
        golden_original_path STRING, golden_translated_path STRING,
        golden_original_hash STRING, golden_translated_hash STRING,
        total_paragraphs INT, certified_paragraphs INT, edits_applied INT,
        distinct_reviewers ARRAY<STRING>,
        published_by STRING, published_at TIMESTAMP, ingested_at TIMESTAMP
    ) USING DELTA
""")
_exec(f"""
    CREATE TABLE IF NOT EXISTS {DELTA_FQN}.silver_review_snapshots (
        snapshot_id STRING, pair_id STRING, snapshot_at TIMESTAMP,
        paragraph_idx INT, status STRING, comment STRING, edited_text STRING,
        reviewer STRING, updated_at TIMESTAMP, ingested_at TIMESTAMP
    ) USING DELTA PARTITIONED BY (pair_id)
""")

# Grant the App SP read/write on the Delta tables.
for stmt in [
    f"GRANT USE CATALOG ON CATALOG {uc_catalog} TO `{sp_uuid}`",
    f"GRANT USE SCHEMA ON SCHEMA {DELTA_FQN} TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.audit_events TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.golden_publications TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.silver_review_snapshots TO `{sp_uuid}`",
]:
    _exec(stmt)
print("ok: Delta mirror tables + grants applied")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-create Volume subdirectories

# COMMAND ----------

vol_root = f"/Volumes/{uc_catalog}/{uc_schema}/{uc_volume_name}"
for sub in ("raw_documents", "translated_inplace", "translated_reviewed", "golden"):
    p = f"{vol_root}/{sub}"
    try:
        w.files.create_directory(p)
        print(f"created: {p}")
    except Exception as e:
        print(f"exists/skipped: {p} ({type(e).__name__})")

# COMMAND ----------

print(f"\npostdeploy complete · app SP {sp_uuid} · catalog {uc_catalog} · schema {uc_schema}")

# COMMAND ----------

# dbutils.notebook.exit raises an exception — kept outside the main flow.
dbutils.notebook.exit(json.dumps({
    "ok": True,
    "app_sp_uuid": sp_uuid,
    "uc_catalog":  uc_catalog,
    "uc_schema":   uc_schema,
    "uc_volume":   uc_volume_name,
    "lakebase_mode": "project" if lakebase_project else "provisioned",
}))
