# Databricks notebook source
# MAGIC %md
# MAGIC # Auto-Translate Watcher
# MAGIC
# MAGIC Orchestration wrapper for the in-place DOCX translation pipeline.
# MAGIC Triggered by file-arrival events on the `raw_documents/` Volume folder.
# MAGIC
# MAGIC On each fire:
# MAGIC 1. Scan `raw_documents/` for `.docx` files
# MAGIC 2. Skip files that already have a translated counterpart in `translated_inplace/`
# MAGIC 3. Upsert a `bronze_documents` Delta row per landing (status `TRANSLATING`)
# MAGIC 4. Call the inner translation notebook **once per unpaired file** via `dbutils.notebook.run`
# MAGIC 5. Update `bronze_documents` to `TRANSLATED` (or `FAILED_TRANSLATION` with error)
# MAGIC
# MAGIC No state lives in this notebook — it's restart-safe: re-running it after a crash
# MAGIC just resumes wherever it left off (idempotent on file presence).

# COMMAND ----------

import datetime
import hashlib
import os
import re
import uuid

# COMMAND ----------

# MAGIC %md
# MAGIC ## Parameters

# COMMAND ----------

dbutils.widgets.text("raw_dir",
    "/Volumes/hls_amer_catalog/guanyu_chen/doc-translation/raw_documents",
    "Raw DOCX folder (watched)")
dbutils.widgets.text("translated_dir",
    "/Volumes/hls_amer_catalog/guanyu_chen/doc-translation/translated_inplace",
    "Translated output folder")
dbutils.widgets.text("target_language", "English", "Target language")
dbutils.widgets.text("model_endpoint", "databricks-claude-sonnet-4-6", "FMAPI model endpoint")
dbutils.widgets.text("max_workers", "8", "Concurrent workers per file")
dbutils.widgets.text("max_pages", "0", "Page limit (0 = all)")
dbutils.widgets.text("translator_notebook_path",
    "/Users/guanyu.chen@databricks.com/Translation PoC/DOCX Inplace Translation",
    "Inner translation notebook")
dbutils.widgets.text("bronze_catalog", "hls_amer_catalog", "Catalog for bronze_documents")
dbutils.widgets.text("bronze_schema",  "guanyu_chen",      "Schema for bronze_documents")
dbutils.widgets.text("glossary_delta_table", "",
    "FQN of the translation_glossary Delta mirror (empty = glossary injection off)")

raw_dir            = dbutils.widgets.get("raw_dir").rstrip("/")
translated_dir     = dbutils.widgets.get("translated_dir").rstrip("/")
target_language    = dbutils.widgets.get("target_language").strip()
model_endpoint     = dbutils.widgets.get("model_endpoint").strip()
max_workers        = dbutils.widgets.get("max_workers").strip()
max_pages          = dbutils.widgets.get("max_pages").strip()
translator_nb_path = dbutils.widgets.get("translator_notebook_path").strip()
bronze_catalog     = dbutils.widgets.get("bronze_catalog").strip()
bronze_schema      = dbutils.widgets.get("bronze_schema").strip()
glossary_delta_table = dbutils.widgets.get("glossary_delta_table").strip()

lang_slug = re.sub(r"[^a-z0-9]+", "_", target_language.lower()).strip("_") or "translated"
bronze_fqn = f"{bronze_catalog}.{bronze_schema}.bronze_documents"

print(f"raw_dir         : {raw_dir}")
print(f"translated_dir  : {translated_dir}")
print(f"target_language : {target_language} (slug={lang_slug})")
print(f"model_endpoint  : {model_endpoint}")
print(f"translator      : {translator_nb_path}")
print(f"bronze table    : {bronze_fqn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure bronze_documents Delta table exists

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {bronze_catalog}.{bronze_schema}")
# Migration for tables created before source_language existed.
try:
    spark.sql(f"ALTER TABLE {bronze_fqn} ADD COLUMNS (source_language STRING)")
except Exception:
    pass  # column already present, or table doesn't exist yet (created below)
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {bronze_fqn} (
        document_id              STRING,
        file_name                STRING,
        input_path               STRING,
        input_hash_sha256        STRING,
        input_size_bytes         BIGINT,
        landed_at                TIMESTAMP,
        first_seen_at            TIMESTAMP,
        translation_status       STRING,
        translation_started_at   TIMESTAMP,
        translation_ended_at     TIMESTAMP,
        translation_output_path  STRING,
        translation_error        STRING,
        translator_run_id        STRING,
        model_endpoint           STRING,
        target_language          STRING,
        source_language          STRING
    )
    USING DELTA
    COMMENT 'Every .docx landing in raw_documents/; orchestration audit + translation status'
    TBLPROPERTIES (
        delta.deletedFileRetentionDuration = 'interval 2557 days',
        delta.logRetentionDuration         = 'interval 2557 days'
    )
""")
print("ok: bronze_documents present")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scan raw_documents → keep only files needing translation

# COMMAND ----------

def sha256_file(local_path: str) -> str:
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _to_fs(p: str) -> str:
    """dbutils returns paths like 'dbfs:/Volumes/...'. open() wants '/Volumes/...'."""
    return p[len("dbfs:"):] if p.startswith("dbfs:") else p

raw_files = []
for entry in dbutils.fs.ls(raw_dir):
    name = entry.name.rstrip("/")
    if not name.lower().endswith(".docx"):
        continue
    if name.startswith("~$"):  # Word lock files
        continue
    raw_files.append({
        "path":  _to_fs(entry.path),
        "name":  name,
        "size":  entry.size,
        "modified_ms": entry.modificationTime,
    })

# Filter out already-translated files
unpaired = []
for f in raw_files:
    stem = f["name"][:-len(".docx")]
    expected_out = f"{translated_dir}/{stem}_translated_{lang_slug}.docx"
    # `dbutils.fs.head` on a Volume from serverless raises an internal
    # error instead of a clean FileNotFoundError, so we can't rely on it.
    # Plain POSIX `os.path.exists` against the FUSE-mounted Volume path
    # is reliable for file presence.
    if os.path.exists(expected_out):
        continue
    unpaired.append({**f, "expected_output": expected_out, "stem": stem})

print(f"raw files   : {len(raw_files)}")
print(f"already done: {len(raw_files) - len(unpaired)}")
print(f"to translate: {len(unpaired)}")
for f in unpaired:
    print(f"  → {f['name']} ({f['size']:,} bytes)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Translate each unpaired file via the inner notebook

# COMMAND ----------

if not unpaired:
    print("Nothing to do.")
    dbutils.notebook.exit("noop")

results = []
for f in unpaired:
    name = f["name"]
    fs_path = f["path"]
    print(f"\n=== {name} ===")

    # Hash + audit row BEFORE we kick off
    try:
        h = sha256_file(fs_path)
    except Exception as e:
        print(f"  ! hash failed: {e}")
        h = None

    doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{fs_path}:{h or ''}"))
    landed = datetime.datetime.fromtimestamp(f["modified_ms"] / 1000, tz=datetime.timezone.utc)
    first_seen = datetime.datetime.now(datetime.timezone.utc)

    # Bronze upsert: TRANSLATING
    spark.sql(f"""
        MERGE INTO {bronze_fqn} AS t
        USING (SELECT
            '{doc_id}' AS document_id,
            '{name}' AS file_name,
            '{fs_path}' AS input_path,
            { "'" + h + "'" if h else "CAST(NULL AS STRING)" } AS input_hash_sha256,
            CAST({f['size']} AS BIGINT) AS input_size_bytes,
            TIMESTAMP '{landed.isoformat()}' AS landed_at,
            TIMESTAMP '{first_seen.isoformat()}' AS first_seen_at,
            'TRANSLATING' AS translation_status,
            current_timestamp() AS translation_started_at,
            '{model_endpoint}' AS model_endpoint,
            '{target_language}' AS target_language
        ) AS s
        ON t.document_id = s.document_id
        WHEN MATCHED THEN UPDATE SET
            translation_status     = s.translation_status,
            translation_started_at = s.translation_started_at,
            translation_error      = NULL,
            translation_ended_at   = NULL,
            model_endpoint         = s.model_endpoint,
            target_language        = s.target_language
        WHEN NOT MATCHED THEN INSERT (
            document_id, file_name, input_path, input_hash_sha256, input_size_bytes,
            landed_at, first_seen_at, translation_status, translation_started_at,
            model_endpoint, target_language
        ) VALUES (
            s.document_id, s.file_name, s.input_path, s.input_hash_sha256, s.input_size_bytes,
            s.landed_at, s.first_seen_at, s.translation_status, s.translation_started_at,
            s.model_endpoint, s.target_language
        )
    """)

    # Direct Volume paths. The inner notebook reads/writes the .docx in
    # whole-file mode (open().read() → BytesIO → ZipFile → BytesIO → open().write()),
    # which is reliable on serverless Volume FUSE — unlike random-access
    # zipfile reads against the FUSE mount, which fail with OSError [Errno 5].
    try:
        inner_result = dbutils.notebook.run(
            translator_nb_path,
            timeout_seconds=7200,
            arguments={
                "input_path":             fs_path,
                "output_dir":             translated_dir,
                "target_language":        target_language,
                "model_endpoint":         model_endpoint,
                "max_workers":            max_workers,
                "max_pages":              max_pages,
                "skip_if_already_target": "true",
                "glossary_delta_table":   glossary_delta_table,
            },
        )
        out_path = f["expected_output"]
        # The inner notebook returns a JSON payload with the auto-detected
        # source language. Parse it defensively — older notebook versions or a
        # non-JSON exit shouldn't fail the run.
        src_lang = None
        try:
            import json as _json
            payload = _json.loads(str(inner_result))
            src_lang = payload.get("source_language_code")
        except Exception:
            pass
        src_lang_sql = f"'{src_lang}'" if src_lang else "CAST(NULL AS STRING)"
        spark.sql(f"""
            UPDATE {bronze_fqn}
            SET translation_status      = 'TRANSLATED',
                translation_ended_at    = current_timestamp(),
                translation_output_path = '{out_path}',
                translator_run_id       = '{str(inner_result)[:64]}',
                source_language         = {src_lang_sql}
            WHERE document_id = '{doc_id}'
        """)
        print(f"  ✓ → {out_path}  (source={src_lang or 'unknown'})")
        results.append({"doc": name, "status": "TRANSLATED", "output": out_path,
                        "source_language": src_lang})
    except Exception as e:
        err = str(e).replace("'", "''")[:1000]
        spark.sql(f"""
            UPDATE {bronze_fqn}
            SET translation_status = 'FAILED_TRANSLATION',
                translation_ended_at = current_timestamp(),
                translation_error = '{err}'
            WHERE document_id = '{doc_id}'
        """)
        print(f"  ✗ FAILED: {e}")
        results.append({"doc": name, "status": "FAILED_TRANSLATION", "error": err})

print(f"\nDone — {len([r for r in results if r['status']=='TRANSLATED'])} ok, "
      f"{len([r for r in results if r['status']=='FAILED_TRANSLATION'])} failed")

dbutils.notebook.exit(str({"processed": len(results), "results": results}))
