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
dbutils.widgets.dropdown("enable_seed_glossary", "false", ["true", "false"],
                         "Load the shipped clinical seed glossary")

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
enable_seed_glossary = dbutils.widgets.get("enable_seed_glossary").lower() == "true"

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
# MAGIC ## Resolve the App service principal UUID + (re-)seed config secrets
# MAGIC
# MAGIC Secret values are seeded by `deploy.sh` step 1 BEFORE `bundle deploy`
# MAGIC (Apps validates secret resource bindings eagerly at app create/update
# MAGIC time, so the keys must exist). But re-seeding here belt-and-braces:
# MAGIC if `deploy.sh` ever produces wrong values (shell-quoting bugs, etc.),
# MAGIC re-running just the postdeploy job repairs them. Python `put_secret`
# MAGIC has no shell-quoting hazards.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

# Apps's SP is created automatically and accessible via the apps API.
app = w.apps.get(name=app_name)
sp_uuid = app.service_principal_client_id
if not sp_uuid:
    raise SystemExit(f"could not resolve service_principal_client_id for app {app_name!r}")
print(f"app SP UUID: {sp_uuid}")

# Re-seed each config secret idempotently. Empty values are valid (e.g.
# lakebase_instance="" when in Project mode); the App's config.py treats
# them as None.
vol_root_value = f"/Volumes/{uc_catalog}/{uc_schema}/{uc_volume_name}"
app_config_secrets = {
    "pg_schema":         pg_schema,
    "lakebase_project":  lakebase_project,
    "lakebase_branch":   lakebase_branch,
    "lakebase_instance": lakebase_instance,
    "volume_root":       vol_root_value,
    "delta_catalog":     uc_catalog,
    "delta_schema":      uc_schema,
}
for k, v in app_config_secrets.items():
    w.secrets.put_secret(scope=secret_scope, key=k, string_value=(v or ""))
    print(f"  re-seeded {secret_scope}/{k} = {v!r}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Provisioned Lakebase: register the App SP role + grant it instance access
# MAGIC
# MAGIC In **Project mode** the bundle's `postgres:` app-resource binding both
# MAGIC registers the App SP as a Postgres role and injects PGHOST/PGUSER/… into
# MAGIC the app at `bundle deploy` time — nothing to do here.
# MAGIC
# MAGIC In **Provisioned mode** we do NOT use the classic `database:` binding at
# MAGIC all. Adding a database resource to an app via the Apps API
# MAGIC (`w.apps.update`) is gated behind workspace-admin authority the deploying
# MAGIC user typically lacks — it fails with *"does not have permission to grant
# MAGIC permissions for added resource: postgres"* even when that user OWNS the
# MAGIC instance + Postgres database, has CAN_MANAGE, and the SP already holds the
# MAGIC exact grants the binding would apply. (Verified empirically 2026-08-23.)
# MAGIC
# MAGIC The binding is only a convenience: it injects PG* env vars and applies a
# MAGIC CONNECT/CREATE grant. We reproduce both without it, using operations the
# MAGIC deploying user CAN perform:
# MAGIC   1. register the App SP as a Postgres role (so the GRANTs below + the
# MAGIC      runtime credential mint have a role to target);
# MAGIC   2. grant the App SP CAN_USE on the instance (so at runtime it can call
# MAGIC      `generate_database_credential`);
# MAGIC   3. GRANT CONNECT on the database to the App SP (the GRANTs cell below).
# MAGIC The app derives PGHOST (instance read/write DNS) + PGUSER
# MAGIC (DATABRICKS_CLIENT_ID) at runtime — see `server/config.py`.
# MAGIC
# MAGIC Idempotent; safe to re-run after every `bundle deploy`.

# COMMAND ----------

if lakebase_instance:
    from databricks.sdk.service import database as _db
    from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

    # (1) Register the App SP as a Postgres role on the instance so the GRANT
    #     statements further down can target it. Idempotent: a duplicate raises
    #     (role already exists), which we swallow.
    try:
        w.database.create_database_instance_role(
            instance_name=lakebase_instance,
            database_instance_role=_db.DatabaseInstanceRole(
                name=sp_uuid,
                identity_type=_db.DatabaseInstanceRoleIdentityType.SERVICE_PRINCIPAL,
            ),
        )
        print(f"ok: registered App SP {sp_uuid} as a Lakebase Postgres role")
    except Exception as e:
        print(f"  App SP role already present (or create skipped): {e}")

    # (2) Grant the App SP CAN_USE on the instance so the running app can mint
    #     its own OAuth DB credential (generate_database_credential). This is a
    #     control-plane permission the deploying user (CAN_MANAGE) can delegate,
    #     unlike the app-resource database binding. Idempotent.
    try:
        w.permissions.update(
            request_object_type="database-instances",
            request_object_id=lakebase_instance,
            access_control_list=[
                AccessControlRequest(
                    service_principal_name=sp_uuid,
                    permission_level=PermissionLevel.CAN_USE,
                ),
            ],
        )
        print(f"ok: granted App SP {sp_uuid} CAN_USE on instance {lakebase_instance!r}")
    except Exception as e:
        print(f"  WARNING: could not grant CAN_USE on instance: {e}")

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
    # Legacy Provisioned mode. Newer SDK/runtime builds require request_id (an
    # idempotency key) on GenerateDatabaseCredential; older ones ignore it, so
    # always pass a fresh UUID for cross-version safety.
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[lakebase_instance],
    )
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
    source              TEXT NOT NULL DEFAULT 'tenant',
    UNIQUE (source_lang, target_lang, model_phrase, correction)
);
ALTER TABLE {pg_schema}.translation_glossary
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'tenant';

CREATE TABLE IF NOT EXISTS {pg_schema}.translation_prompts (
    prompt_id    BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    body         TEXT NOT NULL,
    description  TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

GRANTS_SQL = f"""
GRANT CONNECT ON DATABASE {pg_db} TO "{sp_uuid}";
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

# Seed the built-in default prompt so the upload dialog always has a choice
# (prompt selection is required). Idempotent — only inserts when the table is
# empty. The body text mirrors server/prompts.py:DEFAULT_PROMPT_BODY and the
# notebook's TRANSLATE_SYSTEM fallback; keep the three in sync.
DEFAULT_PROMPT_NAME = "Medical / clinical (default)"
DEFAULT_PROMPT_BODY = (
    "You are a professional medical and clinical document translator working on FDA "
    "regulatory submissions. Translate the user's text to {lang}.\n"
    "STRICT RULES:\n"
    "1. Return ONLY the translated text. No commentary, no quotes, no labels, no explanations.\n"
    "2. Preserve numbers, units, percentages, dates, dosages, and proper nouns exactly.\n"
    "3. Preserve URLs, email addresses, file paths, and code-like tokens unchanged.\n"
    "4. Keep medical terminology accurate and consistent.\n"
    "5. If the input is already in {lang}, return it unchanged.\n"
    "6. If the input is empty, whitespace, or only punctuation/numbers, return it unchanged.\n"
    "7. Do not add or remove leading/trailing whitespace beyond what the source has."
)
with psycopg.connect(conninfo, password=pg_token) as conn:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {pg_schema}.translation_prompts LIMIT 1")
        if cur.fetchone():
            print("prompts already present; seed skipped")
        else:
            cur.execute(f"""
                INSERT INTO {pg_schema}.translation_prompts
                    (name, body, description, created_by, updated_by)
                VALUES (%s, %s, %s, 'system', 'system')
                ON CONFLICT (name) DO NOTHING
            """, (DEFAULT_PROMPT_NAME, DEFAULT_PROMPT_BODY,
                  "Built-in FDA / clinical translation prompt. Seeded automatically."))
            conn.commit()
            print("ok: default translation prompt seeded")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Optional: load the shipped clinical seed glossary
# MAGIC
# MAGIC Controlled by the `enable_seed_glossary` parameter (default off). Loads
# MAGIC public ICH/GCP-standard JA→EN clinical terminology from
# MAGIC `setup/seed_glossary/*.csv` as `source='seed'` rows. Idempotent: the
# MAGIC UNIQUE constraint makes re-runs a no-op. Customer-mined `tenant` entries
# MAGIC and `customer` imports are never overwritten (ON CONFLICT keeps `source`).

# COMMAND ----------

if enable_seed_glossary:
    import csv, glob

    seed_dir = os.path.join(os.path.dirname(os.getcwd()), "setup", "seed_glossary")
    # Notebook CWD varies; resolve the seed dir relative to this file's repo.
    candidates = [
        seed_dir,
        os.path.join(os.getcwd(), "setup", "seed_glossary"),
        os.path.join(os.getcwd(), "seed_glossary"),
    ]
    seed_files: list[str] = []
    for c in candidates:
        seed_files = sorted(glob.glob(os.path.join(c, "*.csv")))
        if seed_files:
            break

    total = 0
    if not seed_files:
        print("WARNING: enable_seed_glossary=true but no seed CSVs found; skipping")
    else:
        with psycopg.connect(conninfo, password=pg_token) as conn:
            with conn.cursor() as cur:
                for path in seed_files:
                    with open(path, encoding="utf-8-sig", newline="") as fh:
                        rows = [r for r in csv.DictReader(fh)]
                    batch = []
                    for r in rows:
                        sl = (r.get("source_lang") or "").strip().lower()
                        tl = (r.get("target_lang") or "").strip().lower()
                        mp = (r.get("model_phrase") or r.get("source_phrase") or "").strip()
                        co = (r.get("correction") or r.get("target_phrase") or "").strip()
                        if sl and tl and mp and co:
                            batch.append((sl, tl, mp, co))
                    if batch:
                        cur.executemany(f"""
                            INSERT INTO {pg_schema}.translation_glossary
                                (source_lang, target_lang, model_phrase, correction,
                                 occurrences, distinct_reviewers, approved, source)
                            VALUES (%s, %s, %s, %s, 1, 1, TRUE, 'seed')
                            ON CONFLICT (source_lang, target_lang, model_phrase, correction)
                            DO NOTHING
                        """, batch)
                        total += len(batch)
                        print(f"  {os.path.basename(path)}: {len(batch)} entries")
            conn.commit()
        print(f"ok: seed glossary loaded ({total} entries offered; duplicates ignored)")
else:
    print("seed glossary disabled (enable_seed_glossary=false)")

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
# Read mirror of translation_glossary — the translation pipeline reads this at
# startup to build its prompt-injection automaton (it can't reach Lakebase).
_exec(f"""
    CREATE TABLE IF NOT EXISTS {DELTA_FQN}.translation_glossary (
        entry_id BIGINT, source_lang STRING, target_lang STRING,
        model_phrase STRING, correction STRING,
        occurrences INT, distinct_reviewers INT,
        last_seen_at TIMESTAMP, created_at TIMESTAMP,
        approved BOOLEAN, source STRING, ingested_at TIMESTAMP
    ) USING DELTA
""")

# Pre-create bronze_documents so the App SP can be granted SELECT here. The
# watcher also CREATE-IF-NOT-EXISTS this table on its first run — but if the
# app tries to read pipeline status before any translation has happened, the
# table wouldn't exist and (more importantly) the SP wouldn't be granted on a
# watcher-owned table. Creating it here with the full schema + the SELECT grant
# makes the /api/documents status view work from the first deploy.
_exec(f"""
    CREATE TABLE IF NOT EXISTS {DELTA_FQN}.bronze_documents (
        document_id STRING, file_name STRING, input_path STRING,
        input_hash_sha256 STRING, input_size_bytes BIGINT,
        landed_at TIMESTAMP, first_seen_at TIMESTAMP,
        translation_status STRING, translation_started_at TIMESTAMP,
        translation_ended_at TIMESTAMP, translation_output_path STRING,
        translation_error STRING, translator_run_id STRING,
        model_endpoint STRING, target_language STRING, source_language STRING,
        selected_prompt_id BIGINT, selected_prompt_name STRING, prompt_text_used STRING,
        submitted_by STRING
    ) USING DELTA
    TBLPROPERTIES (
        delta.deletedFileRetentionDuration = 'interval 2557 days',
        delta.logRetentionDuration         = 'interval 2557 days'
    )
""")
# Migrate deployments created before per-document prompt selection existed.
# Delta's ADD COLUMNS is not idempotent (no IF NOT EXISTS), so this errors when
# the columns already exist — expected on a fresh table (created with them above)
# and on any re-run. Tolerate exactly that case; re-raise anything else.
try:
    _exec(f"""
        ALTER TABLE {DELTA_FQN}.bronze_documents ADD COLUMNS (
            selected_prompt_id BIGINT, selected_prompt_name STRING, prompt_text_used STRING
        )
    """)
    print("ok: bronze_documents prompt columns added")
except RuntimeError as ex:
    if "already exist" in str(ex).lower():
        print("bronze_documents prompt columns already present; skipped")
    else:
        raise

# Migrate deployments created before per-user attribution existed. Same
# non-idempotent ADD COLUMNS caveat as above — tolerate the already-exists case.
try:
    _exec(f"ALTER TABLE {DELTA_FQN}.bronze_documents ADD COLUMNS (submitted_by STRING)")
    print("ok: bronze_documents submitted_by column added")
except RuntimeError as ex:
    if "already exist" in str(ex).lower():
        print("bronze_documents submitted_by column already present; skipped")
    else:
        raise

# Grant the App SP read/write on the Delta tables AND on the UC Volume.
# Volume names with hyphens need backtick quoting in SQL.
volume_fqn = f"{uc_catalog}.{uc_schema}.`{uc_volume_name}`"
for stmt in [
    f"GRANT USE CATALOG ON CATALOG {uc_catalog} TO `{sp_uuid}`",
    f"GRANT USE SCHEMA ON SCHEMA {DELTA_FQN} TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.audit_events TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.golden_publications TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.silver_review_snapshots TO `{sp_uuid}`",
    f"GRANT MODIFY, SELECT ON TABLE {DELTA_FQN}.translation_glossary TO `{sp_uuid}`",
    # bronze_documents is written by the watcher; the app only reads it for the
    # pipeline status view.
    f"GRANT SELECT ON TABLE {DELTA_FQN}.bronze_documents TO `{sp_uuid}`",
    # The App reads .docx from raw_documents/ + translated_inplace/ and writes
    # to translated_reviewed/ + golden/. Both READ + WRITE are needed.
    f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME {volume_fqn} TO `{sp_uuid}`",
]:
    _exec(stmt)
print("ok: Delta mirror tables + Volume grants applied to App SP")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Initial glossary → Delta mirror
# MAGIC
# MAGIC Full-refresh the Delta copy from Lakebase so the translation pipeline
# MAGIC has terminology available on its very first run (before any promotion
# MAGIC has triggered `delta_sync`). Reads directly from Lakebase here (we hold
# MAGIC a psycopg connection) and INSERT OVERWRITEs the Delta table.

# COMMAND ----------

with psycopg.connect(conninfo, password=pg_token) as conn:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT entry_id, source_lang, target_lang, model_phrase, correction,
                   occurrences, distinct_reviewers, last_seen_at, created_at,
                   approved, source
            FROM {pg_schema}.translation_glossary
        """)
        g_cols = [d.name for d in cur.description]
        g_rows = [dict(zip(g_cols, r)) for r in cur.fetchall()]

if not g_rows:
    _exec(f"DELETE FROM {DELTA_FQN}.translation_glossary")
    print("glossary mirror: 0 rows (empty)")
else:
    def _lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return repr(v)
        # datetime → ISO literal; everything else → escaped string
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            return f"TIMESTAMP '{v.isoformat()}'"
        return "'" + str(v).replace("'", "''") + "'"

    CH = 500
    for i in range(0, len(g_rows), CH):
        chunk = g_rows[i : i + CH]
        vals = ",".join(
            "(" + ",".join([
                _lit(e["entry_id"]), _lit(e["source_lang"]), _lit(e["target_lang"]),
                _lit(e["model_phrase"]), _lit(e["correction"]),
                _lit(e["occurrences"]), _lit(e["distinct_reviewers"]),
                _lit(e["last_seen_at"]), _lit(e["created_at"]),
                _lit(e["approved"]), _lit(e.get("source") or "tenant"),
                "current_timestamp()",
            ]) + ")"
            for e in chunk
        )
        verb = "INSERT OVERWRITE" if i == 0 else "INSERT INTO"
        _exec(f"""
            {verb} {DELTA_FQN}.translation_glossary
            (entry_id, source_lang, target_lang, model_phrase, correction,
             occurrences, distinct_reviewers, last_seen_at, created_at,
             approved, source, ingested_at)
            VALUES {vals}
        """)
    print(f"glossary mirror: {len(g_rows)} rows synced to Delta")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-create Volume subdirectories

# COMMAND ----------

vol_root = f"/Volumes/{uc_catalog}/{uc_schema}/{uc_volume_name}"
for sub in ("raw_documents", "translated_inplace", "translated_reviewed", "golden",
            "glossary_imports"):
    p = f"{vol_root}/{sub}"
    try:
        w.files.create_directory(p)
        print(f"created: {p}")
    except Exception as e:
        print(f"exists/skipped: {p} ({type(e).__name__})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Attach the file-arrival trigger to the translation pipeline
# MAGIC
# MAGIC The bundle creates the pipeline job WITHOUT the trigger — Apps' Jobs
# MAGIC API validates the file-arrival URL against an existing Volume +
# MAGIC subdirectory at create time, which races with the bundle's own
# MAGIC volume creation. We add the trigger here, after the subdirs exist.
# MAGIC Idempotent: re-running this just resets the trigger.

# COMMAND ----------

from databricks.sdk.service.jobs import (
    TriggerSettings, FileArrivalTriggerConfiguration, PauseStatus, JobSettings,
    JobAccessControlRequest, JobPermissionLevel,
)

pipeline_jobs = [j for j in w.jobs.list()
                 if j.settings and j.settings.name == "doc-translation · auto-translate pipeline"]
if not pipeline_jobs:
    print("WARNING: pipeline job not found — was bundle deploy run?")
else:
    pj = pipeline_jobs[0]
    trigger_url = f"{vol_root}/raw_documents/"
    w.jobs.update(
        job_id=pj.job_id,
        new_settings=JobSettings(
            trigger=TriggerSettings(
                pause_status=PauseStatus.UNPAUSED,
                file_arrival=FileArrivalTriggerConfiguration(
                    url=trigger_url,
                    min_time_between_triggers_seconds=60,
                    wait_after_last_change_seconds=60,
                ),
            ),
        ),
    )
    print(f"ok: attached file-arrival trigger to job {pj.job_id} watching {trigger_url}")

    # The App SP calls the Jobs API to drive the Processing Status view (is a run
    # active + how long). jobs.list()/list_runs() only return jobs the caller can
    # see, so the SP needs at least CAN_VIEW. update_permissions MERGES (unlike
    # set_permissions, which would replace the owner ACL), so existing grants are
    # preserved. Best-effort: the doc list still renders if this fails.
    try:
        w.jobs.update_permissions(
            job_id=pj.job_id,
            access_control_list=[JobAccessControlRequest(
                service_principal_name=sp_uuid,
                permission_level=JobPermissionLevel.CAN_VIEW,
            )],
        )
        print(f"ok: granted App SP {sp_uuid} CAN_VIEW on job {pj.job_id}")
    except Exception as ex:
        print(f"WARNING: could not grant App SP CAN_VIEW on job {pj.job_id}: {ex}")

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
