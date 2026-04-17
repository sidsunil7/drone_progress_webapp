-- =============================================================
-- Database initialisation: extensions and roles
-- Runs once when the PostgreSQL container is first created.
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gist";

-- Read-only role for dashboards / BI tools
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'drone_readonly') THEN
        CREATE ROLE drone_readonly NOLOGIN;
    END IF;
END
$$;

-- Read-write role for the Flask application
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'drone_app') THEN
        CREATE ROLE drone_app NOLOGIN;
    END IF;
END
$$;

-- Grant default privileges so future tables are accessible
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO drone_readonly;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO drone_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO drone_app;
