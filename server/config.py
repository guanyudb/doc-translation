"""Workspace-portable configuration.

Two Lakebase modes are supported:

  * **Project (Autoscaling)** — the modern path; new workspaces. Set
    `LAKEBASE_PROJECT` + `LAKEBASE_BRANCH` (default `main`). Apps' `postgres:`
    resource binding auto-injects PGHOST / PGPORT / PGDATABASE / PGUSER /
    PGSSLMODE; `server/db.py` mints an OAuth JWT per-connection via
    `/api/2.0/postgres/credentials`.

  * **Provisioned (legacy)** — pre-2026-03-12 workspaces with an existing
    instance. Set `LAKEBASE_INSTANCE` (the instance name). The `database:`
    binding auto-injects PG* env vars; `server/db.py` mints credentials via
    `WorkspaceClient.database.generate_database_credential`.

Detection is explicit: presence of `LAKEBASE_PROJECT` wins, else fall back
to `LAKEBASE_INSTANCE`. Exactly one mode must be set or boot will refuse.
"""
import os
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

# Postgres connection — host etc. are auto-injected by the Apps binding.
PGHOST     = os.environ["PGHOST"]
PGPORT     = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "databricks_postgres")
PGSSLMODE  = os.environ.get("PGSSLMODE", "require")
PGSCHEMA   = os.environ.get("PGSCHEMA", "doc_translation")

# Lakebase mode — Project takes precedence if both are somehow set. Empty
# strings (which secrets-backed env vars produce when the customer left a
# field blank) are normalized to None.
def _maybe(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None

LAKEBASE_PROJECT  = _maybe("LAKEBASE_PROJECT")
LAKEBASE_BRANCH   = _maybe("LAKEBASE_BRANCH") or "main"
LAKEBASE_INSTANCE = _maybe("LAKEBASE_INSTANCE")  # Provisioned fallback

if not LAKEBASE_PROJECT and not LAKEBASE_INSTANCE:
    raise RuntimeError(
        "Lakebase not configured. Set LAKEBASE_PROJECT + LAKEBASE_BRANCH "
        "for a Lakebase Project, or LAKEBASE_INSTANCE for a legacy "
        "Provisioned instance."
    )

USE_LAKEBASE_PROJECT = LAKEBASE_PROJECT is not None

VOLUME_ROOT    = os.environ["VOLUME_ROOT"].rstrip("/")
RAW_DIR        = f"{VOLUME_ROOT}/raw_documents"
TRANSLATED_DIR = f"{VOLUME_ROOT}/translated_inplace"


def get_workspace_client() -> WorkspaceClient:
    if IS_DATABRICKS_APP:
        return WorkspaceClient()
    profile = os.environ.get("DATABRICKS_PROFILE", "vlm")
    return WorkspaceClient(profile=profile)


_w_singleton: WorkspaceClient | None = None


def w() -> WorkspaceClient:
    global _w_singleton
    if _w_singleton is None:
        _w_singleton = get_workspace_client()
    return _w_singleton


def current_pg_user() -> str:
    """Postgres role used for the connection.
    In Databricks Apps, PGUSER is auto-injected (the SP client_id).
    Locally, fall back to the workspace user identity."""
    pguser = os.environ.get("PGUSER")
    if pguser:
        return pguser
    return w().current_user.me().user_name
