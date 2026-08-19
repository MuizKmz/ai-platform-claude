#!/bin/bash
# Runs ONCE, on first container start with an empty data volume.
# To re-run: docker compose down -v  (destroys all data), then docker compose up -d.
#
# This is a .sh rather than a .sql because it needs environment variables
# (the read-only password and the database name). The postgres entrypoint does
# not do variable substitution inside .sql files.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v readonly_password="$POSTGRES_READONLY_PASSWORD" \
     -v db_name="$POSTGRES_DB" <<'SQL'

-- 1. pgvector. Phase 1 stores embeddings here; the extension must exist first.
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Read-only role for the Phase 4 SQL tool.
--    Created NOW so the boundary exists before anything can be tempted to skip it.
--    Security invariant #4: read-only is enforced by Postgres, not by inspecting SQL
--    strings. An AST validator can be tricked by a construction you did not anticipate.
--    A role grant cannot.
SELECT format('CREATE ROLE app_readonly LOGIN PASSWORD %L', :'readonly_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_readonly')
\gexec

-- Grant connect + read on the public schema, and nothing more.
SELECT format('GRANT CONNECT ON DATABASE %I TO app_readonly', :'db_name')
\gexec

GRANT USAGE ON SCHEMA public TO app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_readonly;

-- Tables created later (by Alembic) must also be readable — and ONLY readable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_readonly;

-- Explicitly deny schema creation. This is already the default in PG15+, but
-- stating it makes the intent auditable.
REVOKE CREATE ON SCHEMA public FROM app_readonly;

SQL

echo "init.sh: pgvector extension and app_readonly role are ready."
