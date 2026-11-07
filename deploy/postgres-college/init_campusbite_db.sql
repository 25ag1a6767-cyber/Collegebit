-- CampusBite AI v2.0 — Self-Hosted College PostgreSQL Bootstrap
-- Run as the 'postgres' superuser on college-owned hardware:
--   psql -U postgres -v app_password="'REPLACE_WITH_STRONG_32_CHAR_PASSWORD'" -f deploy/postgres-college/init_campusbite_db.sql

CREATE ROLE campusbite_app WITH LOGIN PASSWORD :app_password NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 80;

CREATE DATABASE campusbite_prod OWNER campusbite_app ENCODING 'UTF8';

\connect campusbite_prod

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO campusbite_app;

ALTER ROLE campusbite_app IN DATABASE campusbite_prod SET statement_timeout = '15s';
ALTER ROLE campusbite_app IN DATABASE campusbite_prod SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE campusbite_app IN DATABASE campusbite_prod SET timezone = 'UTC';
