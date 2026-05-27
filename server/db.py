import psycopg
from psycopg_pool import ConnectionPool
from . import config


class OAuthConnection(psycopg.Connection):
    """Generates a fresh OAuth token per pool connection.
    Lakebase tokens are 1 hour; pool max_lifetime=2700s recycles before expiry."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):
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
