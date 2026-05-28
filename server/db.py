"""Lakebase connection pool with per-connection OAuth credential rotation.

Tokens are 1-hour TTL. Pool `max_lifetime=2700s` (45min) recycles before expiry.

Auth path branches on Lakebase mode (see `config.py`):
  * Project (Autoscaling): POST `/api/2.0/postgres/credentials` with the
    endpoint path. Returns a JWT.
  * Provisioned (legacy): `WorkspaceClient.database.generate_database_credential`
    with the instance name.

Pool lifecycle:
  psycopg-pool 3.2+ enforces a strict state machine — once a pool enters
  the CLOSED state (explicit close, GC, or a fatal connection error during
  open), it can never be reopened. In a multi-session Streamlit Apps
  deployment, the module-level pool can hit this:
    * session 1 opens it
    * GC, app reload, or a transient connect failure transitions it to CLOSED
    * session 2 calls pool.open() → PoolClosed exception
  Mitigation: instead of a static pool object, expose a proxy that lazily
  builds + opens a pool on first attribute access and rebuilds if the
  current pool is closed. Callers keep using `pool.connection()` etc.;
  they don't need to know about the indirection.
"""
import threading
import psycopg
from psycopg_pool import ConnectionPool
from . import config


class OAuthConnection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):
        if config.USE_LAKEBASE_PROJECT:
            # Lakebase Project — mint via raw REST so we work across SDK versions
            # (older bundles in serverless notebook envs lack `w.postgres.*`).
            endpoint = (
                f"projects/{config.LAKEBASE_PROJECT}"
                f"/branches/{config.LAKEBASE_BRANCH}"
                f"/endpoints/primary"
            )
            resp = config.w().api_client.do(
                "POST", "/api/2.0/postgres/credentials",
                body={"endpoint": endpoint},
            )
            kwargs["password"] = resp["token"]
        else:
            cred = config.w().database.generate_database_credential(
                instance_names=[config.LAKEBASE_INSTANCE]
            )
            kwargs["password"] = cred.token
        return super().connect(conninfo, **kwargs)


def _build_conninfo() -> str:
    user = config.current_pg_user()
    return (
        f"dbname={config.PGDATABASE} "
        f"user={user} "
        f"host={config.PGHOST} "
        f"port={config.PGPORT} "
        f"sslmode={config.PGSSLMODE}"
    )


def _build_pool() -> ConnectionPool:
    p = ConnectionPool(
        conninfo=_build_conninfo(),
        connection_class=OAuthConnection,
        min_size=1,
        max_size=8,
        max_lifetime=2700,
        open=False,
    )
    p.open(wait=True, timeout=30.0)
    return p


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    """Return a live, open pool. Rebuild if the current one is closed."""
    global _pool
    with _pool_lock:
        if _pool is None or _pool.closed:
            _pool = _build_pool()
        return _pool


class _PoolProxy:
    """Forwards attribute access to a lazily-built, self-healing pool.
    Lets callers keep writing `pool.connection()` / `pool.open()` etc.
    without caring that the underlying object may have been recreated."""

    def __getattr__(self, name: str):
        return getattr(_get_pool(), name)

    def open(self, *args, **kwargs):
        # Backward-compat shim: app.py calls `pool.open(wait=True, timeout=30.0)`
        # to make startup deterministic. _get_pool() auto-builds + opens on
        # first access; this is effectively a force-init + ack.
        _get_pool()

    @property
    def closed(self) -> bool:
        # Some callers may inspect this; report the real pool's state.
        return _pool is None or _pool.closed


pool = _PoolProxy()
