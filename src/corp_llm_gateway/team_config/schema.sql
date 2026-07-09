-- Plan ref: B5 (GA readiness). Per-team config store for gateway-admin.
-- Parallels the team_config table in tokens/schema.sql; applied idempotently
-- via PostgresTeamConfigStore.init_schema(). CREATE ... IF NOT EXISTS and
-- CREATE OR REPLACE make it safe to run alongside tokens/schema.sql.

CREATE TABLE IF NOT EXISTS team_config (
    team_id              TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    replace_md_path      TEXT,
    profile_ids          TEXT[] NOT NULL DEFAULT '{}'::text[],
    retention_hot_days   INTEGER NOT NULL DEFAULT 90,
    retention_cold_years INTEGER NOT NULL DEFAULT 7,
    fail_policy          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration: a DB created before D2 (or by tokens/schema.sql, which does not
-- define this column) already has team_config, so CREATE TABLE IF NOT EXISTS is
-- a no-op and never adds profile_ids — but the store SELECTs/upserts it. ADD
-- COLUMN IF NOT EXISTS converges fresh and pre-existing DBs (Postgres 9.6+).
ALTER TABLE team_config
    ADD COLUMN IF NOT EXISTS profile_ids TEXT[] NOT NULL DEFAULT '{}'::text[];

-- Keep team_config.updated_at fresh on every UPDATE.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS team_config_set_updated_at ON team_config;
CREATE TRIGGER team_config_set_updated_at
    BEFORE UPDATE ON team_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
