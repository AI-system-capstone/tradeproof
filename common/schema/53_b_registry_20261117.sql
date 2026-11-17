-- B M2 metadata only; C/D schemas remain unchanged. Stop B before applying.
BEGIN;
SELECT pg_advisory_xact_lock(20261114, 50);
DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM b_registry_schema_migrations WHERE version='20261117_01') THEN
        RETURN;
    END IF;
    ALTER TABLE bundles ADD COLUMN validation_id VARCHAR(80);
    ALTER TABLE bundles ADD COLUMN validation JSON;
    INSERT INTO b_registry_schema_migrations(version) VALUES ('20261117_01');
END
$migration$;
COMMIT;
