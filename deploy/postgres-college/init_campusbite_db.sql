
CREATE ROLE campusbite_app WITH LOGIN PASSWORD :app_password NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 80;

CREATE DATABASE campusbite_prod OWNER campusbite_app ENCODING 'UTF8';

\connect campusbite_prod

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO campusbite_app;

ALTER ROLE campusbite_app IN DATABASE campusbite_prod SET statement_timeout = '15s';
ALTER ROLE campusbite_app IN DATABASE campusbite_prod SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE campusbite_app IN DATABASE campusbite_prod SET timezone = 'UTC';
