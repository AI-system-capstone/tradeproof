"""Explicit PostgreSQL migrations; never called automatically by the API."""
from pathlib import Path

from sqlalchemy.engine import make_url

MIGRATION = Path(__file__).resolve().parents[1] / "common/schema/50_b_registry_20261114.sql"


def migrate(database_url):
    import psycopg

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("migrate-db requires the existing PostgreSQL DATABASE_URL; SQLite is test-only")
    # The SQL owns BEGIN/COMMIT, advisory locking and the version ledger.
    params = url.translate_connect_args(username="user", database="dbname")
    params.update(url.query)
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(MIGRATION.read_text(encoding="utf-8"), prepare=False)
