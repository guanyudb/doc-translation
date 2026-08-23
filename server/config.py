"""Workspace-portable configuration.

Two Lakebase modes are supported:

  * **Project (Autoscaling)** — the modern path; new workspaces. Set
    `LAKEBASE_PROJECT` + `LAKEBASE_BRANCH` (default `main`). Apps' `postgres:`
    resource binding auto-injects PGHOST / PGPORT / PGDATABASE / PGUSER /
    PGSSLMODE; `server/db.py` mints an OAuth JWT per-connection via
    `/api/2.0/postgres/credentials`.

  * **Provisioned (legacy)** — pre-2026-03-12 workspaces with an existing
    instance. Set `LAKEBASE_INSTANCE` (the instance name). We do NOT use the
    classic `database:` app-resource binding here: binding it via the Apps API
    requires workspace-admin authority the deploying user typically lacks (it
    fails with "does not have permission to grant permissions for added
    resource: postgres" even for the instance owner). So `setup/postdeploy.py`
    instead registers the app SP as a Postgres role + grants it directly, and
    this module DERIVES the connection coordinates at runtime: PGHOST from the
    instance's read/write DNS, PGUSER from the app SP's client id
    (`DATABRICKS_CLIENT_ID`, always injected into Apps). `server/db.py` mints
    credentials via `WorkspaceClient.database.generate_database_credential`.

Detection is explicit: presence of `LAKEBASE_PROJECT` wins, else fall back
to `LAKEBASE_INSTANCE`. Exactly one mode must be set or boot will refuse.
"""
import os
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

# Postgres connection. PGHOST/PGUSER are injected by a `postgres:`/`database:`
# app-resource binding when one exists (Project mode, or Provisioned mode on an
# admin-deployed workspace). In Provisioned mode without a binding they're
# absent and derived lazily — see pg_host() / current_pg_user() below.
PGHOST     = os.environ.get("PGHOST")
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

# Deploy-time branding (all optional). Customers set these via the secret scope
# (see deploy.sh / resources/app.yml / app.yaml). Unset — including the " "
# placeholder deploy.sh writes for blank values — falls back to the defaults:
# the built-in lucide icon and the "Doc Translation Review" title.
APP_TITLE    = _maybe("APP_TITLE") or "Doc Translation Review"
APP_LOGO_URL = _maybe("APP_LOGO_URL")  # e.g. "/brand-logo.png"; None → lucide icon
APP_LOGO_ALT = _maybe("APP_LOGO_ALT")  # image label / alt text; None → falls back to title


def _maybe_int(name: str) -> int | None:
    """Positive integer from an optional env var, else None (unset / blank /
    non-numeric / non-positive)."""
    v = _maybe(name)
    if v is None:
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


# Optional logo dimensions in CSS pixels. When set, they're applied to the
# <img>; when unset (either), the logo renders at its natural size, unconstrained.
APP_LOGO_WIDTH  = _maybe_int("APP_LOGO_WIDTH")
APP_LOGO_HEIGHT = _maybe_int("APP_LOGO_HEIGHT")


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


_pg_host_cache: str | None = None


def pg_host() -> str:
    """Resolve the Postgres host.

    When a `postgres:`/`database:` app-resource binding exists (Project mode, or
    an admin-deployed Provisioned workspace) PGHOST is injected as an env var and
    we use it directly. In Provisioned mode WITHOUT a binding — the usual case,
    since a non-admin deployer can't create the classic database binding — PGHOST
    is absent, so we derive it from the instance's read/write DNS and cache it."""
    global _pg_host_cache
    if PGHOST:
        return PGHOST
    if _pg_host_cache:
        return _pg_host_cache
    if LAKEBASE_INSTANCE:
        inst = w().database.get_database_instance(name=LAKEBASE_INSTANCE)
        _pg_host_cache = inst.read_write_dns
        return _pg_host_cache
    raise RuntimeError(
        "PGHOST is not set and cannot be derived (no LAKEBASE_INSTANCE). "
        "In Project mode the postgres binding must inject PGHOST."
    )


def current_pg_user() -> str:
    """Postgres role used for the connection.

    The role is the app service principal, whose Postgres role name is its OAuth
    client id. A `postgres:`/`database:` binding injects that as PGUSER; without a
    binding (Provisioned mode) we read the SP's client id from DATABRICKS_CLIENT_ID
    (always injected into Databricks Apps). Locally, fall back to the workspace
    user identity."""
    pguser = os.environ.get("PGUSER")
    if pguser:
        return pguser
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    if client_id:
        return client_id
    return w().current_user.me().user_name
