"""Lakebase connection pool with per-connection OAuth credential rotation.

Tokens are 1-hour TTL. Pool `max_lifetime=2700s` (45min) recycles before expiry.

Auth path branches on Lakebase mode (see `config.py`):
  * Project (Autoscaling): POST `/api/2.0/postgres/credentials` with the
    endpoint path. Returns a JWT.
  * Provisioned (legacy): `WorkspaceClient.database.generate_database_credential`
    with the instance name.
"""
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


user = config.current_pg_user()
conninfo = (
    f"dbname={config.PGDATABASE} "
    f"user={user} "
    f"host={config.PGHOST} "
    f"port={config.PGPORT} "
    f"sslmode={config.PGSSLMODE}"
)

pool = ConnectionPool(
    conninfo=conninfo,
    connection_class=OAuthConnection,
    min_size=1,
    max_size=8,
    max_lifetime=2700,
    open=False,
)
