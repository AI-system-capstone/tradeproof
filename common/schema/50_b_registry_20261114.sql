BEGIN;
SELECT pg_advisory_xact_lock(20261114, 50);
CREATE TABLE IF NOT EXISTS b_registry_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
DO $migration$
DECLARE
    entry RECORD;
BEGIN
    IF EXISTS (SELECT 1 FROM b_registry_schema_migrations WHERE version = '20261114_01') THEN
        RETURN;
    END IF;
    -- Preserve bundle IDs and rows, renaming only B-owned reference columns.
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()
               AND table_name='bundles' AND column_name='model_component_id') THEN
        ALTER TABLE bundles RENAME COLUMN model_component_id TO model_id;
        ALTER TABLE bundles RENAME COLUMN list_component_id TO list_id;
    END IF;
CREATE TABLE IF NOT EXISTS ai_models (
    model_id VARCHAR(80) PRIMARY KEY,
    artifact_uri VARCHAR(500) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE IF NOT EXISTS sanctions_lists (
    list_id VARCHAR(80) PRIMARY KEY,
    artifact_uri VARCHAR(500) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);


    IF to_regclass('components') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM components WHERE component_type NOT IN ('ocr_model', 'sanctions_list')) THEN
            RAISE EXCEPTION 'Unsupported legacy component type; migration rolled back';
        END IF;
        INSERT INTO ai_models (model_id, artifact_uri, created_at)
            SELECT component_id, artifact_uri, created_at FROM components WHERE component_type='ocr_model'
            ON CONFLICT (model_id) DO NOTHING;
        INSERT INTO sanctions_lists (list_id, artifact_uri, created_at)
            SELECT component_id, artifact_uri, created_at FROM components WHERE component_type='sanctions_list'
            ON CONFLICT (list_id) DO NOTHING;
        IF EXISTS (SELECT 1 FROM components c JOIN ai_models a ON a.model_id=c.component_id
                   WHERE c.component_type='ocr_model' AND a.artifact_uri<>c.artifact_uri)
           OR EXISTS (SELECT 1 FROM components c JOIN sanctions_lists l ON l.list_id=c.component_id
                      WHERE c.component_type='sanctions_list' AND l.artifact_uri<>c.artifact_uri) THEN
            RAISE EXCEPTION 'Conflicting model/list IDs; migration rolled back';
        END IF;
    END IF;
CREATE TABLE IF NOT EXISTS registry_lock (
	id SERIAL NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS bundles (
	bundle_id VARCHAR(36) NOT NULL,
	model_id VARCHAR(80) NOT NULL,
	list_id VARCHAR(80) NOT NULL,
	status VARCHAR(30) NOT NULL,
	previous_active_id VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (bundle_id),
	FOREIGN KEY(model_id) REFERENCES ai_models (model_id),
	FOREIGN KEY(list_id) REFERENCES sanctions_lists (list_id),
	FOREIGN KEY(previous_active_id) REFERENCES bundles (bundle_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_active ON bundles (status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS imports (
	import_id VARCHAR(36) NOT NULL,
	artifact_uri VARCHAR(500) NOT NULL UNIQUE,
	status VARCHAR(30) NOT NULL,
	reason VARCHAR(500),
	model_id VARCHAR(80),
	list_id VARCHAR(80),
	bundle_id VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (import_id),
	CONSTRAINT ck_import_single_asset CHECK (model_id IS NULL OR list_id IS NULL),
	FOREIGN KEY(model_id) REFERENCES ai_models (model_id),
	FOREIGN KEY(list_id) REFERENCES sanctions_lists (list_id),
	FOREIGN KEY(bundle_id) REFERENCES bundles (bundle_id)
);



    ALTER TABLE imports ADD COLUMN IF NOT EXISTS model_id VARCHAR(80);
    ALTER TABLE imports ADD COLUMN IF NOT EXISTS list_id VARCHAR(80);
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()
               AND table_name='imports' AND column_name='component_id') THEN
        UPDATE imports i SET model_id=c.component_id FROM components c
            WHERE i.component_id=c.component_id AND c.component_type='ocr_model';
        UPDATE imports i SET list_id=c.component_id FROM components c
            WHERE i.component_id=c.component_id AND c.component_type='sanctions_list';
        IF EXISTS (SELECT 1 FROM imports WHERE component_id IS NOT NULL AND model_id IS NULL AND list_id IS NULL) THEN
            RAISE EXCEPTION 'Unmapped legacy import reference; migration rolled back';
        END IF;
    END IF;
    -- Replace legacy component FKs, preserving the bundle/self-reference FKs.
    FOR entry IN SELECT conname, conrelid::regclass AS relation FROM pg_constraint
        WHERE contype='f' AND conrelid IN ('bundles'::regclass, 'imports'::regclass)
          AND confrelid IN (to_regclass('components'), 'ai_models'::regclass, 'sanctions_lists'::regclass)
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', entry.relation, entry.conname);
    END LOOP;
    ALTER TABLE bundles ADD CONSTRAINT fk_b_registry_model FOREIGN KEY(model_id) REFERENCES ai_models(model_id);
    ALTER TABLE bundles ADD CONSTRAINT fk_b_registry_list FOREIGN KEY(list_id) REFERENCES sanctions_lists(list_id);
    ALTER TABLE imports ADD CONSTRAINT fk_import_model FOREIGN KEY(model_id) REFERENCES ai_models(model_id);
    ALTER TABLE imports ADD CONSTRAINT fk_import_list FOREIGN KEY(list_id) REFERENCES sanctions_lists(list_id);
    ALTER TABLE imports DROP CONSTRAINT IF EXISTS ck_import_single_asset;
    ALTER TABLE imports ADD CONSTRAINT ck_import_single_asset CHECK(model_id IS NULL OR list_id IS NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_import_artifact_uri ON imports(artifact_uri);

    ALTER TABLE ai_models DROP COLUMN IF EXISTS logical_version, DROP COLUMN IF EXISTS sha256, DROP COLUMN IF EXISTS files;
    ALTER TABLE sanctions_lists DROP COLUMN IF EXISTS logical_version, DROP COLUMN IF EXISTS sha256, DROP COLUMN IF EXISTS files;
    ALTER TABLE bundles DROP COLUMN IF EXISTS rules_component_id, DROP COLUMN IF EXISTS rules_version,
        DROP COLUMN IF EXISTS history, DROP COLUMN IF EXISTS replay_status, DROP COLUMN IF EXISTS replay_reason;
    ALTER TABLE imports DROP COLUMN IF EXISTS component_id, DROP COLUMN IF EXISTS package_hash, DROP COLUMN IF EXISTS steps;
    ALTER TABLE registry_lock DROP COLUMN IF EXISTS revision;
    INSERT INTO registry_lock (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
    INSERT INTO b_registry_schema_migrations(version) VALUES ('20261114_01');
    -- Legacy components is retained as an unused archive; no DROP TABLE/CASCADE.
END;
$migration$;
COMMIT;
