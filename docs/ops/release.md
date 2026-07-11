# Release workflow

How a change gets from a topic branch to a published GHCR image. The whole
loop is scripted (`scripts/release/`) — this doc explains the branch rule,
merge policy, versioning scheme, and what each script does.

## Prerequisites

- Authenticated `gh` CLI (`gh auth login`) — `ship.sh` and `cut-rc.sh` use it
  to create PRs, poll checks, merge, and watch the publish run.
- `jq` — `cut-rc.sh` parses `gh pr view --json ...` output.
- `docker` — `gates.sh` builds both image profiles locally before you ship.
- `python3` with `PyYAML` — `gates.sh`'s workflow-YAML-parse gate.

## Branch rule

Topic branches are `fix/*`, `feature/*`, or `chore/*`. Every topic branch
opens a PR against the single `release/*` line (currently `release/1.0.x`) —
never against `main` directly. `main` only receives release→main merges; it
never takes a topic-branch PR.

`ship.sh` enforces the branch-name prefix and refuses to push otherwise; it
also refuses if it finds zero or more than one `release/*` branch on origin.

## Merge policy

The `release/*` branch ruleset requires **1 approving review + linear
history** (no merge commits — rebase or squash only). Merges are performed by
a **human operator**, not a script. On a solo-maintainer repo that means:

```
gh pr merge <n> --admin --rebase
```

`cut-rc.sh` attempts a plain `gh pr merge <n> --rebase` first; if the ruleset
blocks it (expected on a solo repo with no second reviewer), it tells you to
run the `--admin --rebase` form yourself and then re-run `cut-rc.sh` — it
detects the already-merged PR and resumes from the tag step.

## Versioning: tag-only rc scheme

Static version fields (`pyproject.toml`, the Helm `Chart.yaml`,
`__version__`) stay pinned to the **bare target version** (e.g. `1.0.0`) for
the whole release cycle — there is no per-rc file bump. Merged PRs simply
batch on the release branch until you're ready to cut a candidate.

- `bash scripts/release/cut-rc.sh v1.0.0-rc.4` tags an rc explicitly, or
- `bash scripts/release/cut-rc.sh next` auto-suggests and cuts the next `rc.N`
  from the existing `v<ver>-rc.*` tags, or
- `bash scripts/release/cut-rc.sh` (no args) just prints the suggested next
  tag and exits 2 — a dry check, touches nothing.
- GA is the bare `v<ver>` tag (no `-rc.N` suffix). It flips the `:latest` /
  `:latest-en` image tags forward (the `build-image.yml` guard only enables
  `latest` when the tag name has no `-`).

## Changelog

There is no `[Unreleased]` section. Every notable PR adds its bullet directly
under the upcoming version's heading (e.g. `[1.0.0]`) in the same PR that
lands the change. Cutting an rc never touches `CHANGELOG.md` — the per-rc
delta is the auto-generated GitHub Release notes between tags (see
`github-release` below). The GA tag's Release page may later swap the
generated notes for the curated section via `gh release edit --notes-file`.

## Delivery loop

Four steps, run in order:

1. **`bash scripts/release/gates.sh`** — pre-push gate battery. Builds both
   image profiles (`ru-en`, `en`) locally against `Dockerfile.gateway`, runs
   a runtime import + NER-load check inside each built image, smoke-tests
   the entrypoint (`--help`), and parses `build-image.yml` as YAML. Run this
   before shipping — it catches image-build breakage before CI does.

2. **`bash scripts/release/ship.sh <topic-branch>`** — pushes the branch,
   opens a PR against the discovered `release/*` branch (`gh pr create
   --fill`), waits for checks to register, then watches them to green (or
   fails loudly with the final check states).

3. **Merge** — `cut-rc.sh` (step 4) attempts a plain rebase merge itself; the
   `release/*` ruleset is expected to block it for a solo maintainer (no
   second approving review), and the script exits with a hint instead of
   forcing it through. At that point a **human operator** decides and runs
   `gh pr merge <n> --admin --rebase` themselves — `cut-rc.sh` never invokes
   `--admin`. Re-running `cut-rc.sh` afterward detects the already-merged PR
   and resumes from the tag step.

4. **`bash scripts/release/cut-rc.sh [<tag> | next]`** — validates the PR
   for the current branch is green and targets the release branch, merges it
   if still open (rebase), resolves the landed commit, creates and pushes an
   annotated tag, then finds and watches the `build-gateway-image` run for
   that tag.

A `v*` tag push (from step 4) triggers two independent workflows:

- **`build-gateway-image`** (`build-image.yml`) — builds and pushes the
  multi-arch (amd64+arm64) GHCR images for both NER profiles.
- **`github-release`** (`release.yml`) — creates the GitHub Release page with
  generated notes (`--prerelease` for `-rc.N` tags). Idempotent: re-pushing
  the same tag is a no-op if the release already exists.

They run in parallel — the Release page can appear a few minutes before the
images finish building.

## Image tags

`ghcr.io/jlam-err/corp-llm-gateway`, multi-arch (`linux/amd64` +
`linux/arm64`) on every `v*` tag push:

| variant                    | tag pattern         | example               |
| -------------------------- | -------------------- | ---------------------- |
| ru-en (default, bilingual) | `:<ver>[-rc.N]`       | `:1.0.0`, `:1.0.0-rc.3` |
| en (EN-only NER, smaller)  | `:<ver>[-rc.N]-en`    | `:1.0.0-en`             |
| short commit sha           | `:sha-<short-sha>`   | `:sha-a1b2c3d`          |
| latest GA (ru-en)          | `:latest`             | moves only on bare `v<ver>` tags |
| latest GA (en)             | `:latest-en`          | moves only on bare `v<ver>` tags |

`:latest` / `:latest-en` never move on an rc tag — only a final `v<ver>`
push flips them.

```
# Default bilingual (RU+EN) image, pinned to a release
docker pull ghcr.io/jlam-err/corp-llm-gateway:1.0.0

# EN-only variant (no RU NER stack)
docker pull ghcr.io/jlam-err/corp-llm-gateway:1.0.0-en

# A release candidate
docker pull ghcr.io/jlam-err/corp-llm-gateway:1.0.0-rc.3

# Latest GA (bilingual)
docker pull ghcr.io/jlam-err/corp-llm-gateway:latest

# Exact build, by commit
docker pull ghcr.io/jlam-err/corp-llm-gateway:sha-a1b2c3d
```
