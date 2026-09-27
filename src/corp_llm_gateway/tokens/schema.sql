-- Plan ref: M2-1
-- Postgres schema for corp tokens + per-team config.
-- Applied via standard migration tool against the M0-5 Postgres HA pair.

CREATE TABLE IF NOT EXISTS corp_tokens (
    corp_token           TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL,
    team_id              TEXT NOT NULL,
    scopes               TEXT[] NOT NULL DEFAULT '{}',
    issued_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at           TIMESTAMPTZ,
    last_used_at         TIMESTAMPTZ,
    oidc_issuer          TEXT,
    oidc_subject         TEXT,
    oidc_jti             TEXT
);

CREATE INDEX IF NOT EXISTS corp_tokens_user_id_idx
    ON corp_tokens (user_id);

CREATE INDEX IF NOT EXISTS corp_tokens_team_id_idx
    ON corp_tokens (team_id);

CREATE INDEX IF NOT EXISTS corp_tokens_revoked_at_idx
    ON corp_tokens (revoked_at)
    WHERE revoked_at IS NOT NULL;

-- Migration: developer token issuance (POST /internal/issue-token) records the
-- Keycloak identity each token was minted for. Additive and idempotent: existing
-- rows and CLI-issued tokens keep NULLs, which the per-subject cap ignores. Re-run
-- this file on an existing DB before enabling CORP_GATEWAY_ISSUE_OIDC_ISSUER.
ALTER TABLE corp_tokens ADD COLUMN IF NOT EXISTS oidc_issuer TEXT;
ALTER TABLE corp_tokens ADD COLUMN IF NOT EXISTS oidc_subject TEXT;
ALTER TABLE corp_tokens ADD COLUMN IF NOT EXISTS oidc_jti TEXT;

-- A Keycloak jti mints at most one corp token: the replay backstop behind the
-- store's in-transaction check. NULLs (CLI-issued) never conflict.
CREATE UNIQUE INDEX IF NOT EXISTS corp_tokens_oidc_jti_key
    ON corp_tokens (oidc_jti);

CREATE INDEX IF NOT EXISTS corp_tokens_oidc_subject_idx
    ON corp_tokens (oidc_issuer, oidc_subject, revoked_at);

-- Per-team config: replace.md location, retention overrides, fail-policy
-- overrides per the M4 fail-policy matrix. Plan ref: M2-4, M3-7, M4-7.
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

-- Migration: converge a team_config created before D2 (profile_ids added later).
-- Idempotent, so it is safe whether this file or team_config/schema.sql ran first.
ALTER TABLE team_config
    ADD COLUMN IF NOT EXISTS profile_ids TEXT[] NOT NULL DEFAULT '{}'::text[];

-- Trigger: keep team_config.updated_at fresh.
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

-- Enforcement reminder (not schema-level): the corp_token column and the
-- developer's BYOK Authorization header must NEVER appear in any audit
-- record. Invariants live in M1-14 / M2-7 / M3-10. The audit-schema doc
-- (docs/audit-schema.md) defines the ALWAYS / NEVER / CONDITIONAL fields.
