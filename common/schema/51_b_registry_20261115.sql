-- Apply after 50_b_registry_20261114.sql with B stopped.
BEGIN;
SELECT pg_advisory_xact_lock(20261114, 50);
DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM b_registry_schema_migrations WHERE version = '20261115_01') THEN
        RETURN;
    END IF;
    -- Old validated meant package validation, not the additional C validation.
    UPDATE bundles SET status = 'candidate' WHERE status = 'validated';
    UPDATE bundles SET status = 'cancelled' WHERE status = 'rolled_back';
    -- Keep legacy history; never silently approve an unprocessed package.
    UPDATE imports SET status = 'REJECTED',
        reason = 'Legacy pending import: resubmit the ZIP for synchronous validation',
        completed_at = CURRENT_TIMESTAMP WHERE status = 'RECEIVED';
    CREATE UNIQUE INDEX uq_registry_canary ON bundles(status) WHERE status = 'canary';
    INSERT INTO b_registry_schema_migrations(version) VALUES ('20261115_01');
END
$migration$;
COMMIT;
