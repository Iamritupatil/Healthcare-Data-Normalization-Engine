#!/bin/sh
# Creates the three database roles, and a separate database for the test suite.
# Runs once, as the Postgres superuser:
#   - automatically on first start of the db container (docker-entrypoint-initdb.d), or
#   - by CI against its Postgres service.
#
# The superuser bypasses row-level security even with FORCE, so nothing in the app ever
# logs in as it. The app logs in as medvora_app, which owns nothing and cannot bypass RLS.
set -eu

: "${DB_OWNER_PASSWORD:?DB_OWNER_PASSWORD must be set}"
: "${DB_APP_PASSWORD:?DB_APP_PASSWORD must be set}"
DB_NAME="${POSTGRES_DB:-medvora}"
SUPERUSER="${POSTGRES_USER:-postgres}"

psql -v ON_ERROR_STOP=1 --username "$SUPERUSER" --dbname "$DB_NAME" \
     -v owner_password="$DB_OWNER_PASSWORD" \
     -v app_password="$DB_APP_PASSWORD" <<'SQL'
CREATE ROLE medvora_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE medvora_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE medvora_system NOLOGIN NOBYPASSRLS;

-- The owner may hand functions to medvora_system (see migrations/rls.py).
GRANT medvora_system TO medvora_owner;
SQL

# The test suite truncates tables, so it gets its own database and never touches yours.
psql -v ON_ERROR_STOP=1 --username "$SUPERUSER" --dbname "$DB_NAME" \
     -c "CREATE DATABASE \"${DB_NAME}_test\" OWNER medvora_owner"

for database in "$DB_NAME" "${DB_NAME}_test"; do
    psql -v ON_ERROR_STOP=1 --username "$SUPERUSER" --dbname "$database" \
         -v db_name="$database" <<'SQL'
ALTER DATABASE :"db_name" OWNER TO medvora_owner;
ALTER SCHEMA public OWNER TO medvora_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO medvora_app;
SQL
done
