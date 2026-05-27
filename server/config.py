import os
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

PGHOST = os.environ["PGHOST"]
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "databricks_postgres")
PGSSLMODE = os.environ.get("PGSSLMODE", "require")
PGSCHEMA = os.environ.get("PGSCHEMA", "doc_translation")
LAKEBASE_INSTANCE = os.environ["LAKEBASE_INSTANCE"]
VOLUME_ROOT = os.environ["VOLUME_ROOT"].rstrip("/")
RAW_DIR = f"{VOLUME_ROOT}/raw_documents"
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
