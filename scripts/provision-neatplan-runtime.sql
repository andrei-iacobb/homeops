-- One-time provisioning, run as a database administrator with psql -X -f.
-- Connect to the database containing the existing neatplan schema.
-- This deliberately creates a NOLOGIN role without a password. Before enabling
-- LOGIN, store a generated credential in Vaultwarden and the encrypted app
-- secret. Keep the old migration credential in a separate secret mounted only
-- by the migrator. Never expose it through the web container's envFrom.
-- Verify runtime CRUD and denied access to other app schemas before switching.
-- Existing owners, memberships, other schemas and PUBLIC grants are untouched.
-- Checks below cover this database. Check PUBLIC grants in other databases too
-- before enabling LOGIN; CONNECT is commonly granted to PUBLIC cluster-wide.
\set ON_ERROR_STOP on

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'neatplan') THEN
    RAISE EXCEPTION 'The existing neatplan schema is required';
  END IF;
END
$$;

-- Fail if this name is already in use; do not repurpose an existing account.
CREATE ROLE neatplan_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT;
ALTER ROLE neatplan_runtime SET search_path = pg_catalog, neatplan;
SELECT format('GRANT CONNECT ON DATABASE %I TO neatplan_runtime', current_database()) \gexec
GRANT USAGE ON SCHEMA neatplan TO neatplan_runtime;

SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO neatplan_runtime', n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'neatplan' AND c.relkind IN ('r', 'p')
  AND c.relname <> '_prisma_migrations' \gexec
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA neatplan TO neatplan_runtime;

-- Migrations run as the existing owners. Grant runtime access to future objects
-- created by those owners, only in this schema.
-- If recovery deliberately recreates _prisma_migrations, revoke all privileges
-- on that table from neatplan_runtime again before starting the web container.
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA neatplan GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO neatplan_runtime', rolname)
FROM pg_roles WHERE oid IN (
  SELECT nspowner FROM pg_namespace WHERE nspname = 'neatplan'
  UNION
  SELECT c.relowner FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'neatplan' AND c.relkind IN ('r', 'p', 'S')
) \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA neatplan GRANT USAGE, SELECT ON SEQUENCES TO neatplan_runtime', rolname)
FROM pg_roles WHERE oid IN (
  SELECT nspowner FROM pg_namespace WHERE nspname = 'neatplan'
  UNION
  SELECT c.relowner FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'neatplan' AND c.relkind IN ('r', 'p', 'S')
) \gexec

-- Privileges inherited from PUBLIC also apply to new roles. Refuse provisioning
-- if they expose another app's objects or permit persistent DDL; do not revoke them
-- here because that could disrupt unrelated apps.
DO $$
BEGIN
  IF has_database_privilege('neatplan_runtime', current_database(), 'CREATE')
    OR EXISTS (
      SELECT FROM pg_namespace
      WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
        AND has_schema_privilege('neatplan_runtime', oid, 'CREATE')
    ) THEN
    RAISE EXCEPTION 'PUBLIC grants permit persistent DDL; resolve separately';
  END IF;
  IF EXISTS (
    SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname !~ '^pg_'
      AND n.nspname NOT IN ('information_schema', 'neatplan')
      AND has_schema_privilege('neatplan_runtime', n.oid, 'USAGE')
      AND CASE WHEN c.relkind IN ('r', 'p', 'v', 'm', 'f') THEN (
        has_table_privilege('neatplan_runtime', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_any_column_privilege('neatplan_runtime', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')
      ) ELSE false END
  ) THEN
    RAISE EXCEPTION 'PUBLIC grants expose another schema; resolve separately';
  END IF;
  IF EXISTS (
    SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname !~ '^pg_'
      AND n.nspname NOT IN ('information_schema', 'neatplan')
      AND has_schema_privilege('neatplan_runtime', n.oid, 'USAGE')
      AND CASE WHEN c.relkind = 'S'
        THEN has_sequence_privilege('neatplan_runtime', c.oid, 'USAGE,SELECT,UPDATE')
        ELSE false END
  ) THEN
    RAISE EXCEPTION 'PUBLIC grants expose another schema sequence; resolve separately';
  END IF;
END
$$;

COMMIT;
