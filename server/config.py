"""Workspace-portable configuration.

Lakebase runs on **Autoscaling Projects** (the Provisioned tier is retired —
see the databricks-lakebase skill). Set `LAKEBASE_PROJECT` + `LAKEBASE_BRANCH`
(default `production`). The app's `postgres:` resource binding auto-injects
PGHOST / PGPORT / PGDATABASE / PGUSER / PGSSLMODE, and it registers the app
service principal as a Postgres role so `setup/postdeploy.py` can GRANT to it.
`server/db.py` mints an OAuth JWT per connection via
`/api/2.0/postgres/credentials`.
"""
import os
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

# Postgres connection coordinates — injected by the `postgres:` app-resource
# binding (see resources/app.yml).
PGHOST     = os.environ.get("PGHOST")
PGPORT     = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "databricks_postgres")
PGSSLMODE  = os.environ.get("PGSSLMODE", "require")
PGSCHEMA   = os.environ.get("PGSCHEMA", "doc_translation")


# Empty strings (which secrets-backed env vars produce when the customer left a
# field blank) are normalized to None.
def _maybe(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None

LAKEBASE_PROJECT = _maybe("LAKEBASE_PROJECT")
LAKEBASE_BRANCH  = _maybe("LAKEBASE_BRANCH") or "production"

if not LAKEBASE_PROJECT:
    raise RuntimeError(
        "Lakebase not configured. Set LAKEBASE_PROJECT (+ LAKEBASE_BRANCH) for "
        "the Autoscaling Lakebase Project this app connects to."
    )

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


def pg_host() -> str:
    """Postgres host — injected as PGHOST by the `postgres:` app-resource
    binding. Raises if absent (the binding is missing or not yet propagated)."""
    if PGHOST:
        return PGHOST
    raise RuntimeError(
        "PGHOST is not set. In Lakebase Project mode the `postgres:` app-resource "
        "binding (resources/app.yml) must inject it — check the binding is active."
    )


def current_pg_user() -> str:
    """Postgres role for the connection — the app SP, whose role name is its
    OAuth client id. The `postgres:` binding injects it as PGUSER; fall back to
    DATABRICKS_CLIENT_ID (always injected into Apps), then the local user."""
    pguser = os.environ.get("PGUSER")
    if pguser:
        return pguser
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    if client_id:
        return client_id
    return w().current_user.me().user_name
