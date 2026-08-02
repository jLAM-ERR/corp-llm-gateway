#!/bin/sh
# Creates litellm's OWN database on the shared postgres instance (virtual-key
# store + UI accounts), separate from $POSTGRES_DB (our tokens/team_config
# schema — see ../README.md). Committed (not *.sql, so not git-ignored): it
# creates structure only, no data, so it cannot drift from source. Runs once,
# on first init of an empty data volume, same as every docker-entrypoint-
# initdb.d script — safe to assume "litellm" doesn't exist yet.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE DATABASE litellm;
EOSQL
