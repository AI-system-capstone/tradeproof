-- Apply with B stopped, after 50 and 51. No new tables.
BEGIN;
SELECT pg_advisory_xact_lock(20261114, 50);
DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM b_registry_schema_migrations WHERE version = '20261116_01') THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM bundles WHERE status = 'canary') THEN
        RAISE EXCEPTION 'Finish or cancel the running canary before applying migration 52';
    END IF;
    ALTER TABLE bundles ADD COLUMN base_bundle_id VARCHAR(36) REFERENCES bundles(bundle_id);
    ALTER TABLE bundles ADD COLUMN history JSON NOT NULL DEFAULT '[]';
    ALTER TABLE imports ADD COLUMN filename VARCHAR(255);
    ALTER TABLE imports ALTER COLUMN artifact_uri DROP NOT NULL;
    -- Old candidates have no reliable creation baseline: do not infer it from today's active bundle.
    -- Old transition actors/reasons were not recorded and cannot be reconstructed.
    INSERT INTO b_registry_schema_migrations(version) VALUES ('20261116_01');
END
$migration$;
COMMIT;
