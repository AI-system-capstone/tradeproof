"""Use an existing disposable PostgreSQL database; isolate every case in a schema."""
import os
import uuid

import pytest

from b_registry.migrations import MIGRATION, migrate


def test_migration_requires_postgresql():
    with pytest.raises(ValueError, match="PostgreSQL"):
        migrate("sqlite:///:memory:")


@pytest.mark.parametrize("legacy", [False, True])
def test_postgresql_forward_migration(legacy):
    dsn = os.getenv("B_REGISTRY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("B_REGISTRY_TEST_DATABASE_URL required for PostgreSQL migration integration test")
    import psycopg
    from psycopg import sql

    schema = "b_migration_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            conn.execute("CREATE TABLE unrelated_data(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO unrelated_data VALUES(42)")
            if legacy:
                conn.execute("""
                    CREATE TABLE components(component_id VARCHAR(80) PRIMARY KEY, component_type TEXT,
                        logical_version TEXT, artifact_uri TEXT, sha256 TEXT, files JSONB, created_at TIMESTAMPTZ);
                    INSERT INTO components VALUES
                        ('model-v3','ocr_model','model-v3','file://model','old','[]',now()),
                        ('list-v14','sanctions_list','list-v14','file://list','old','[]',now());
                    CREATE TABLE bundles(bundle_id VARCHAR(36) PRIMARY KEY,
                        model_component_id VARCHAR(80) REFERENCES components(component_id),
                        list_component_id VARCHAR(80) REFERENCES components(component_id),
                        rules_component_id TEXT, status TEXT, previous_active_id VARCHAR(36) REFERENCES bundles,
                        history JSONB, replay_status TEXT, replay_reason TEXT, created_at TIMESTAMPTZ);
                    INSERT INTO bundles VALUES('B-7','model-v3','list-v14','rules-v2','active',NULL,'[]','submitted',NULL,now());
                    CREATE TABLE imports(import_id VARCHAR(36) PRIMARY KEY, package_hash TEXT UNIQUE,
                        artifact_uri VARCHAR(500), status TEXT, reason TEXT, steps JSONB,
                        component_id VARCHAR(80) REFERENCES components(component_id),
                        bundle_id VARCHAR(36) REFERENCES bundles, created_at TIMESTAMPTZ, completed_at TIMESTAMPTZ);
                    INSERT INTO imports VALUES('imp-1','old','file://model','APPROVED',NULL,'[]','model-v3','B-7',now(),now());
                    CREATE TABLE registry_lock(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
                    INSERT INTO registry_lock VALUES(1,19);
                """)
            script = MIGRATION.read_text(encoding="utf-8")
            conn.execute(script, prepare=False)
            conn.execute(script, prepare=False)
            assert conn.execute("SELECT count(*) FROM b_registry_schema_migrations").fetchone()[0] == 1
            assert conn.execute("SELECT id FROM unrelated_data").fetchone()[0] == 42
            from b_registry.db import Base
            for table in Base.metadata.sorted_tables:
                columns = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (schema, table.name)).fetchall()
                assert {row[0] for row in columns} == set(table.columns.keys())
            if legacy:
                assert conn.execute("SELECT model_id,list_id,status FROM bundles WHERE bundle_id='B-7'").fetchone() == ("model-v3", "list-v14", "active")
                assert conn.execute("SELECT model_id,list_id,bundle_id FROM imports").fetchone() == ("model-v3", None, "B-7")
                assert conn.execute("SELECT artifact_uri FROM ai_models").fetchone()[0] == "file://model"
                assert conn.execute("SELECT count(*) FROM components").fetchone()[0] == 2
        finally:
            conn.execute("ROLLBACK")
            conn.execute("SET search_path TO public")
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
