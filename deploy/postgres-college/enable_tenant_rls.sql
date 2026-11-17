-- CollegeBits Multi-Tenant PostgreSQL Row-Level Security (RLS) Hardening Script
-- Run as PostgreSQL superuser against the production database after `alembic upgrade head`.
-- Enforces strict tenant_id isolation at the database layer for non-BYPASSRLS app roles.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'collegebits_app') THEN
    CREATE ROLE collegebits_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
$$;

ALTER TABLE IF EXISTS saas_tenant_orders ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_saas_orders ON saas_tenant_orders;
CREATE POLICY tenant_isolation_saas_orders ON saas_tenant_orders
  USING (
    current_setting('app.current_tenant_id', true) IS NULL
    OR current_setting('app.current_tenant_id', true) = ''
    OR tenant_id = current_setting('app.current_tenant_id', true)::integer
  );

ALTER TABLE IF EXISTS saas_campuses ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_saas_campuses ON saas_campuses;
CREATE POLICY tenant_isolation_saas_campuses ON saas_campuses
  USING (
    current_setting('app.current_tenant_id', true) IS NULL
    OR current_setting('app.current_tenant_id', true) = ''
    OR tenant_id = current_setting('app.current_tenant_id', true)::integer
  );

ALTER TABLE IF EXISTS saas_outlets ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_saas_outlets ON saas_outlets;
CREATE POLICY tenant_isolation_saas_outlets ON saas_outlets
  USING (
    current_setting('app.current_tenant_id', true) IS NULL
    OR current_setting('app.current_tenant_id', true) = ''
    OR tenant_id = current_setting('app.current_tenant_id', true)::integer
  );

ALTER TABLE IF EXISTS admin_invitations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_admin_invitations ON admin_invitations;
CREATE POLICY tenant_isolation_admin_invitations ON admin_invitations
  USING (
    current_setting('app.current_tenant_id', true) IS NULL
    OR current_setting('app.current_tenant_id', true) = ''
    OR tenant_id = current_setting('app.current_tenant_id', true)::integer
  );

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO collegebits_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO collegebits_app;
