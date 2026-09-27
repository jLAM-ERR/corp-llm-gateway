"""Single source of truth for every config key the gateway reads.

``KEYS`` enumerates each setting once (name, default, whether it is required, and
how to validate it). Values still resolve through :mod:`corp_llm_gateway.config`
(env → ``$CORP_LLM_GATEWAY_CONFIG_FILE`` → ``~/.corp-llm-gateway`` → ``/etc`` →
default): this module NEVER reads ``os.environ`` and NEVER uses pydantic's native
env/dotenv sourcing, so the documented resolution chain (CLAUDE.md) is preserved.
``validate()`` feeds config-resolved values INTO pydantic; pydantic only
types/validates.

``validate()`` is the startup fail-fast — it refuses to serve traffic with a
missing-required or malformed setting. Notably it hard-fails on an unset
``CORP_LLM_ENDPOINT`` when the oracle is enabled (``CORP_LLM_ORACLE_ENABLED``,
default on), which otherwise silently defaults to a non-routable placeholder
and only surfaces as a 503 on the first gazetteer-hit request (``bootstrap.py``
only warns). With the oracle disabled, no endpoint is required — local-first
only (solo/local mode) — but ``CORP_LLM_LOCAL_FIRST`` must then be on, else
validation refuses to boot as a no-op sanitizer.

pydantic is the authoritative validator when importable (prod / CI 3.12); the
package must also import on 3.14 where pydantic is absent (the NER
graceful-degradation venv), so the same required/choice checks are implemented in
plain Python and run when pydantic is missing. Both paths give identical
pass/fail outcomes for the required-endpoint, choice, conditional-credential and
oversize cases.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from corp_llm_gateway import config

# Allowed values for the two selector keys. The pydantic model restates these as
# Literals; test_settings pins them equal so the two paths cannot drift.
AUTH_PROVIDERS: tuple[str, ...] = ("noop", "bearer", "mtls", "oidc", "apikey")
AUDIT_SINKS: tuple[str, ...] = ("stdout", "langfuse", "list")
METRICS_EXPORTERS: tuple[str, ...] = ("noop", "prometheus")
TRACING_EXPORTERS: tuple[str, ...] = ("noop",)

_FLAG_FALSE: frozenset[str] = frozenset({"0", "false", "no", "off", ""})

# Placeholder default, overridden per deployment (like CORP_GATEWAY_URL). Points
# at the gateway's own version endpoint so the check works on the restricted
# network; ops set the real host via config.
_DEFAULT_LATEST_URL = "https://gateway.corp.lan/version"


def _as_flag(value: str | None) -> bool:
    """Lenient flag parse mirroring the runtime accessors (anything not falsey → True)."""
    if value is None:
        return False
    return value.strip().lower() not in _FLAG_FALSE


def parse_flag(value: str | None) -> bool:
    """Public seam for :func:`_as_flag` — the single source of truth other
    modules (e.g. ``bootstrap.py``) must reuse so flag parsing never forks."""
    return _as_flag(value)


@dataclass(frozen=True)
class Key:
    """One config key: how to resolve, whether it's required, and how to check it."""

    name: str
    default: str | None = None
    required: bool = False
    secret: bool = False
    flag: bool = False
    choices: tuple[str, ...] | None = None
    required_when: tuple[str, str] | None = None
    help: str = ""


KEYS: tuple[Key, ...] = (
    # ── Laptop CLIs (corp-llm-gateway status / -proxy) ───────────────────────
    Key("CORP_GATEWAY_URL", default="https://gateway.corp.lan", help="gateway base URL"),
    Key("CORP_GATEWAY_TOKEN_FILE", default="~/.corp-llm-gateway/token", help="corp token path"),
    Key("CORP_GATEWAY_LATEST_URL", default=_DEFAULT_LATEST_URL, help="latest-version URL"),
    # ── Deployment environment ───────────────────────────────────────────────
    Key("CORP_ENV", default="", help="deployment marker; 'prod'/'production' arms F9 guards"),
    # ── Corp LLM oracle endpoint / model ─────────────────────────────────────
    Key(
        "CORP_LLM_ENDPOINT",
        required=False,
        help="corp vLLM base URL (…/v1); required when the oracle is enabled — no routable default",
    ),
    Key("CORP_LLM_MODEL", default="GLM-5.1-AWQ", help="oracle model name"),
    Key("CORP_LLM_AUTH_TOKEN", secret=True, default="", help="legacy oracle bearer token"),
    # ── Detection pipeline knobs ─────────────────────────────────────────────
    Key("CORP_LLM_RULES_DIR", default="/etc/corp-llm-gateway/rules", help="replace.md dir"),
    Key("CORP_LLM_LOCAL_FIRST", flag=True, default="1", help="enable the local-first cascade"),
    Key("CORP_LLM_GAZETTEER", flag=True, default="1", help="enable the gazetteer detector"),
    Key("CORP_LLM_BLOCK_PAYLOADS", flag=True, default="1", help="Stage 0 payload classifier"),
    Key("CORP_LLM_DLP_GUARD", flag=True, default="1", help="Stage 5 DLP egress guard"),
    Key("CORP_LLM_DLP_CANARIES", default="", help="comma-separated DLP canary regexes"),
    Key(
        "CORP_LLM_FORWARD_CHATGPT_AUTH",
        flag=True,
        default="0",
        help="forward allowlisted Codex OAuth headers to ChatGPT backend",
    ),
    Key(
        "CORP_LLM_STRIP_INBOUND_HEADERS",
        flag=True,
        default="1",
        help="strip inbound wire headers (Host, User-Agent, Content-Length, ...) before "
        "forwarding to upstream; the guardrail sets data['headers'] unconditionally, so "
        "this matters regardless of litellm's forward_client_headers_to_llm_api. Off "
        "sends the client's Content-Length with a longer sanitized body and the provider "
        "truncates the request",
    ),
    Key(
        "CORP_LLM_FORWARD_ANTHROPIC_AUTH",
        flag=True,
        default="0",
        help="forward an Anthropic subscription (sk-ant-oat) OAuth bearer upstream; "
        "mutually exclusive with CORP_LLM_FORWARD_CHATGPT_AUTH",
    ),
    # Not a gateway knob — litellm's own virtual-key switch. Registered so it
    # resolves through the config chain (never a bare os.environ read) and so
    # `config check` can refuse it alongside a forward-auth bridge.
    # No default: `None` has to mean "absent", because litellm reads a blank
    # assignment as a set-and-empty master key, not as an unset one.
    Key(
        "LITELLM_MASTER_KEY",
        secret=True,
        help="litellm virtual-key master key; must be unset while a forward-auth bridge is on",
    ),
    # Choices validated by normalize_oversize_policy (see _check_oversize), not
    # the generic choice check, so the canonical error message is used once.
    Key("CORP_LLM_OVERSIZE_POLICY", default="fail-closed", help="oversize-leaf policy (F1)"),
    Key("CORP_LLM_OVERSIZE_DELIVER_TEAMS", default="", help="teams allowed deliver-flag"),
    Key("CORP_LLM_REQUIRE_NER", flag=True, default="0", help="fail closed when NER absent (F2)"),
    # Choices validated by normalize_oracle_trigger (see _check_oracle_trigger),
    # not the generic choice check, because sampled:<pct> is not a fixed literal.
    Key(
        "CORP_LLM_ORACLE_TRIGGER",
        default="gazetteer_hit",
        help="when the conditional oracle runs (F3): "
        "gazetteer_hit | any_local_finding | sampled:<pct> | always",
    ),
    Key(
        "CORP_LLM_ORACLE_ENABLED",
        flag=True,
        default="1",
        help="enable the corp-LLM oracle; off = local-first cascade only (solo/local mode)",
    ),
    Key("CORP_LLM_LOG_LEVEL", default="INFO", help="log level"),
    # ── Corp NER service (detectors/corp_ner.py, B4) ─────────────────────────
    # Default off so existing deploys are unaffected. No REQUIRE flag: corp NER
    # is fail-closed unconditionally — an unscanned text never reads as clean.
    Key("CORP_NER_ENABLED", flag=True, default="0", help="enable the corp NER detector"),
    Key(
        "CORP_NER_ENDPOINT",
        help="corp NER base URL (client appends /v1/analyze); required when CORP_NER_ENABLED=1",
    ),
    Key(
        "CORP_NER_TIMEOUT_S",
        default="30",
        help="corp NER request timeout, seconds; deliberately under the service's 60s",
    ),
    Key("CORP_NER_MAX_TEXTS", default="256", help="corp NER texts per batch (service limit)"),
    Key(
        "CORP_NER_MAX_INPUT_CHARS",
        default="200000",
        help="corp NER chars per batch (service limit); a longer single text fails closed",
    ),
    Key("CORP_NER_CA_BUNDLE", help="PEM CA bundle path; verify corp-NER TLS against it"),
    # ── Backends ─────────────────────────────────────────────────────────────
    Key("CORP_LLM_PG_DSN", secret=True, help="Postgres DSN; unset → in-memory stores"),
    Key("REDIS_URL", secret=True, help="Redis URL for the mapping store; unset → in-memory"),
    # ── TLS to the corp LLM ──────────────────────────────────────────────────
    Key("CORP_LLM_CA_BUNDLE", help="PEM CA bundle path; verify corp-LLM TLS against it"),
    Key("SSL_VERIFY", default="true", help="'false' disables corp-LLM TLS verification"),
    # ── Corp-LLM auth provider (auth/factory.py) ─────────────────────────────
    Key("CORP_LLM_AUTH_PROVIDER", default="noop", choices=AUTH_PROVIDERS, help="oracle auth mode"),
    Key(
        "CORP_LLM_BEARER_TOKEN",
        secret=True,
        required_when=("CORP_LLM_AUTH_PROVIDER", "bearer"),
        help="bearer token (auth provider = bearer)",
    ),
    Key(
        "CORP_LLM_MTLS_CERT",
        required_when=("CORP_LLM_AUTH_PROVIDER", "mtls"),
        help="client cert (auth provider = mtls)",
    ),
    Key(
        "CORP_LLM_MTLS_KEY",
        required_when=("CORP_LLM_AUTH_PROVIDER", "mtls"),
        help="client key (auth provider = mtls)",
    ),
    Key(
        "CORP_LLM_OIDC_ISSUER",
        required_when=("CORP_LLM_AUTH_PROVIDER", "oidc"),
        help="OIDC issuer (auth provider = oidc)",
    ),
    Key(
        "CORP_LLM_OIDC_CLIENT_ID",
        required_when=("CORP_LLM_AUTH_PROVIDER", "oidc"),
        help="OIDC client id (auth provider = oidc)",
    ),
    Key(
        "CORP_LLM_OIDC_CLIENT_SECRET",
        secret=True,
        required_when=("CORP_LLM_AUTH_PROVIDER", "oidc"),
        help="OIDC client secret (auth provider = oidc)",
    ),
    Key("CORP_LLM_API_KEY_HEADER", default="X-Api-Key", help="api-key header name"),
    Key(
        "CORP_LLM_API_KEY",
        secret=True,
        required_when=("CORP_LLM_AUTH_PROVIDER", "apikey"),
        help="api key (auth provider = apikey)",
    ),
    # ── Audit sink (audit/factory.py) ────────────────────────────────────────
    Key("CORP_AUDIT_SINK", default="stdout", choices=AUDIT_SINKS, help="audit sink kind"),
    Key(
        "CORP_LANGFUSE_URL",
        required_when=("CORP_AUDIT_SINK", "langfuse"),
        help="Langfuse URL (audit sink = langfuse)",
    ),
    Key(
        "CORP_LANGFUSE_PUBLIC_KEY",
        secret=True,
        required_when=("CORP_AUDIT_SINK", "langfuse"),
        help="Langfuse public key (audit sink = langfuse)",
    ),
    Key(
        "CORP_LANGFUSE_SECRET_KEY",
        secret=True,
        required_when=("CORP_AUDIT_SINK", "langfuse"),
        help="Langfuse secret key (audit sink = langfuse)",
    ),
    # ── Operator RBAC (auth/rbac.py) ─────────────────────────────────────────
    Key("CORP_GATEWAY_RBAC", flag=True, default="1", help="enforce gateway:operator RBAC"),
    Key("CORP_GATEWAY_OIDC_KEY", secret=True, default="", help="RBAC JWT RS256 public key (F11)"),
    Key("CORP_GATEWAY_OIDC_AUDIENCE", default="", help="expected RBAC JWT audience (aud); F11"),
    Key("CORP_GATEWAY_OIDC_ISSUER", default="", help="expected RBAC JWT issuer (iss); F11"),
    Key("CORP_GATEWAY_ADMIN_TOKEN", secret=True, default="", help="operator JWT for gateway-admin"),
    # ── Developer token issuance (tokens/oidc_verifier.py, healthz/server.py) ─
    # Unset issuer ⇒ issuance disabled; the rest is validated only when it is set.
    Key(
        "CORP_GATEWAY_ISSUE_OIDC_ISSUER",
        default="",
        help="Keycloak realm issuer URL for /internal/issue-token; unset disables issuance",
    ),
    Key(
        "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE",
        default="",
        help="expected issuance JWT aud; must differ from CORP_GATEWAY_OIDC_AUDIENCE",
    ),
    Key(
        "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID",
        default="",
        help="Keycloak client id install.sh uses; the JWT azp must equal it",
    ),
    Key(
        "CORP_GATEWAY_ISSUE_OIDC_JWKS_URL",
        default="",
        help="JWKS URL; unset → {issuer}/protocol/openid-connect/certs; HTTPS in prod",
    ),
    Key("CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM", default="groups", help="claim holding groups"),
    Key(
        "CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP",
        help="ordered group → team_id TOML table (config file only); first mapped group wins",
    ),
    Key(
        "CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM",
        default="preferred_username",
        help="claim used as user_id (falls back to sub)",
    ),
    Key("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", default="30", help="issued corp token lifetime"),
    Key("CORP_GATEWAY_ISSUE_MAX_ACTIVE", default="2", help="live corp tokens per (iss, sub)"),
    Key(
        "CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS",
        default="600",
        help="min seconds between issuances per (iss, sub)",
    ),
    Key("CORP_GATEWAY_ISSUE_MAX_INFLIGHT", default="4", help="concurrent issuance requests"),
    Key("CORP_GATEWAY_ISSUE_RATE_PER_MINUTE", default="30", help="issuance requests per minute"),
    # ── Providers (providers/registry.py) ────────────────────────────────────
    Key("CORP_ALLOW_V2_PROVIDERS", flag=True, default="0", help="allow non-v1 providers"),
    # ── Profiles (profiles/) ─────────────────────────────────────────────────
    Key("CORP_PROFILE_ROOT", default="", help="profile bundle root dir; unset → shipped defaults"),
    Key(
        "CORP_PROFILE_REQUIRE_SIGNATURE",
        flag=True,
        default="0",
        help="fail closed unless a profile is signed (D6; gated on offline PKI)",
    ),
    # ── Metrics / tracing exporters (metrics/) ───────────────────────────────
    Key(
        "CORP_METRICS_EXPORTER",
        default="noop",
        choices=METRICS_EXPORTERS,
        help="metrics exporter: noop (default) | prometheus (needs the [metrics] extra)",
    ),
    Key(
        "CORP_TRACING_EXPORTER",
        default="noop",
        choices=TRACING_EXPORTERS,
        help="tracing exporter (reserved): noop",
    ),
    # ── Server entrypoint (asgi.py / serve.py) ───────────────────────────────
    Key(
        "CORP_LLM_LITELLM_CONFIG",
        default="/etc/litellm/config.yaml",
        help="path to litellm's proxy config YAML; the entrypoint refuses to "
        "start (exit 78) when it is missing, unreadable or not YAML",
    ),
    Key(
        "CORP_LLM_SERVE_HOST",
        # The container's own interface, as litellm's CLI defaults to.
        default="0.0.0.0",
        help="address `python -m corp_llm_gateway.serve` binds (litellm CLI default)",
    ),
    Key(
        "CORP_LLM_SERVE_PORT",
        default="4000",
        help="port `python -m corp_llm_gateway.serve` binds (litellm CLI default)",
    ),
    # ── Route gate (route_gate/) ─────────────────────────────────────────────
    # The only knob the gate has. It can widen the table with PASSTHROUGH routes
    # an operator owns; it can never add REWRITTEN and there is no off switch.
    Key(
        "CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH",
        default="",
        help="extra passthrough routes for the route gate, 'METHOD /path' "
        "comma- or newline-separated (e.g. 'GET /internal/ops-status'); "
        "PASSTHROUGH only — it can never admit a route as rewritten",
    ),
    # ── Test-data allowlist (sanitizer/allowlist.py) ─────────────────────────
    Key("CORP_LLM_TESTDATA_ALLOWLIST", default="", help="inline never-redact test values"),
    Key("CORP_LLM_TESTDATA_ALLOWLIST_FILE", default="", help="never-redact test values file"),
    # ── Solo/local dev auth seam (tokens/middleware.py) ──────────────────────
    Key(
        "CORP_LLM_DEV_TEAM_TOKEN",
        secret=True,
        default="",
        help="DEV-ONLY: seeds an in-memory X-Corp-Auth token for team 'local-dev' "
        "(solo/local compose quickstart, no Postgres); ignored with a warning "
        "when CORP_LLM_PG_DSN is set or CORP_ENV is prod/production",
    ),
    # ── Demo (docker compose only) ───────────────────────────────────────────
    Key("DEMO_TEAM_TOKEN", default="demo-team-token", help="demo-stack team token"),
)

_BY_NAME: dict[str, Key] = {k.name: k for k in KEYS}


def all_keys() -> tuple[str, ...]:
    """Every config key the app reads, in declaration order."""
    return tuple(k.name for k in KEYS)


def is_secret(name: str) -> bool:
    """Whether a key's value must never be echoed (e.g. by ``gateway-admin config check``)."""
    key = _BY_NAME.get(name)
    return key.secret if key is not None else False


class ConfigError(RuntimeError):
    """Raised by :func:`validate` when config is missing-required or malformed.

    ``problems`` lists every issue found (validation does not stop at the first).
    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` sites still
    catch it.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"invalid gateway configuration:\n  - {joined}")


@dataclass(frozen=True)
class Settings:
    """Validated, config-resolved view of every key. Built by :func:`validate`."""

    values: Mapping[str, str | None]

    def __getitem__(self, name: str) -> str | None:
        return self.values.get(name)

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def flag(self, name: str) -> bool:
        return _as_flag(self.values.get(name))


def _resolve() -> dict[str, str | None]:
    """Resolve every key through the config chain (env → file → default)."""
    return {k.name: config.get(k.name, k.default) for k in KEYS}


def _check_required(values: Mapping[str, str | None], problems: list[str]) -> None:
    for key in KEYS:
        if key.required and not values.get(key.name):
            problems.append(
                f"{key.name}: required — set the env var or add it to the config file "
                f"($CORP_LLM_GATEWAY_CONFIG_FILE / ~/.corp-llm-gateway/config.toml). {key.help}"
            )


def _check_choices(values: Mapping[str, str | None], problems: list[str]) -> None:
    for key in KEYS:
        value = values.get(key.name)
        if key.choices is not None and value and value.strip().lower() not in key.choices:
            problems.append(f"{key.name}={value!r} is invalid; expected one of {list(key.choices)}")


def _check_conditional(values: Mapping[str, str | None], problems: list[str]) -> None:
    for key in KEYS:
        when = key.required_when
        if when is None or values.get(key.name):
            continue
        sel_key, sel_val = when
        if (values.get(sel_key) or "").strip().lower() == sel_val:
            problems.append(f"{key.name}: required when {sel_key}={sel_val}. {key.help}")


def _check_oversize(values: Mapping[str, str | None], problems: list[str]) -> None:
    from corp_llm_gateway.payload.size_threshold import normalize_oversize_policy

    try:
        normalize_oversize_policy(values.get("CORP_LLM_OVERSIZE_POLICY"))
    except ValueError as exc:
        problems.append(f"CORP_LLM_OVERSIZE_POLICY: {exc}")


def _check_route_gate_extras(values: Mapping[str, str | None], problems: list[str]) -> None:
    from corp_llm_gateway.route_gate.table import parse_extras

    try:
        parse_extras(values.get("CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH"))
    except ValueError as exc:
        problems.append(f"CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH: {exc}")


def _check_litellm_config(values: Mapping[str, str | None], problems: list[str]) -> None:
    """Check litellm's own proxy config when it is present.

    Absence is NOT a problem here: `config check` runs on operator laptops where
    nothing is mounted at /etc/litellm. The entrypoint (`asgi.py`) is what
    refuses to serve without it, with exit 78.
    """
    from pathlib import Path

    from corp_llm_gateway.litellm_config import DEFAULT_CONFIG_PATH
    from corp_llm_gateway.litellm_config import problems as litellm_problems

    raw = values.get("CORP_LLM_LITELLM_CONFIG") or DEFAULT_CONFIG_PATH
    problems.extend(litellm_problems(Path(raw.strip()), require_file=False))


def check_serve_port(values: Mapping[str, str | None], problems: list[str]) -> None:
    """Public: `serve.py` runs this one check on its own before it binds."""
    raw = (values.get("CORP_LLM_SERVE_PORT") or "").strip()
    if raw and not (raw.isdigit() and 1 <= int(raw) <= 65535):
        problems.append(f"CORP_LLM_SERVE_PORT={raw!r} is not a TCP port number")


def _check_oracle_trigger(values: Mapping[str, str | None], problems: list[str]) -> None:
    from corp_llm_gateway.sanitizer.orchestrator import normalize_oracle_trigger

    try:
        normalize_oracle_trigger(values.get("CORP_LLM_ORACLE_TRIGGER"))
    except ValueError as exc:
        problems.append(f"CORP_LLM_ORACLE_TRIGGER: {exc}")


def _check_oracle_endpoint(values: Mapping[str, str | None], problems: list[str]) -> None:
    """CORP_LLM_ENDPOINT is required only when the oracle is enabled.

    Deliberately plain-Python (not pydantic field logic, not ``required_when``):
    ``required_when`` is exact-match string equality, while the oracle flag
    parses leniently via :func:`_as_flag` — using it here would let
    ``true``/``yes``/``on`` silently skip the requirement.
    """
    if not _as_flag(values.get("CORP_LLM_ORACLE_ENABLED")):
        return
    key = _BY_NAME["CORP_LLM_ENDPOINT"]
    if not values.get(key.name):
        problems.append(
            f"{key.name}: required — set the env var or add it to the config file "
            f"($CORP_LLM_GATEWAY_CONFIG_FILE / ~/.corp-llm-gateway/config.toml). {key.help}"
        )


def _check_corp_ner_endpoint(values: Mapping[str, str | None], problems: list[str]) -> None:
    """CORP_NER_ENDPOINT is required only when corp NER is enabled.

    Plain-Python for the same reason as :func:`_check_oracle_endpoint`:
    ``required_when`` is exact-match string equality, while the flag parses
    leniently, so ``true``/``yes``/``on`` would silently skip the requirement.
    """
    if not _as_flag(values.get("CORP_NER_ENABLED")):
        return
    key = _BY_NAME["CORP_NER_ENDPOINT"]
    if not values.get(key.name):
        problems.append(
            f"{key.name}: required — set the env var or add it to the config file "
            f"($CORP_LLM_GATEWAY_CONFIG_FILE / ~/.corp-llm-gateway/config.toml). {key.help}"
        )


# Shared with bootstrap.build_guardrail(), which enforces the same invariant
# inline (non-k8s boot paths never run `validate()`); keep one message string.
NO_OP_SANITIZER_MESSAGE = (
    "CORP_LLM_ORACLE_ENABLED=0 requires CORP_LLM_LOCAL_FIRST=1 — oracle disabled requires "
    "the local-first cascade (regex+checksum+NER floor); gazetteer/rules alone are "
    "insufficient without the oracle backstop"
)


# Shared with bootstrap.build_guardrail(), which enforces the same invariant at
# runtime (compose/demo/bare-litellm boots never run `validate()`).
FORWARD_AUTH_EXCLUSIVE_MESSAGE = (
    "CORP_LLM_FORWARD_CHATGPT_AUTH and CORP_LLM_FORWARD_ANTHROPIC_AUTH are mutually "
    "exclusive — both consume the same inbound Authorization bearer, so with both on "
    "the developer's credential would be lifted onto whichever upstream the request "
    "happens to reach. Provider-keyed selection of the two bridges is a v2 follow-up; "
    "enable exactly one in v1"
)


def forward_auth_conflict(*, chatgpt: bool, anthropic: bool) -> str | None:
    """The one place the exclusivity rule is expressed.

    Takes already-parsed flags so `build_guardrail()` can apply it to values that
    came from explicit kwargs rather than config.

    Precedence: an explicit `build_guardrail()` kwarg resolves its flag BEFORE the
    pair is checked, so a kwarg that turns one bridge off leaves a legal pair even
    when both env vars are on (exactly one bridge ends up live — no credential
    confusion; bootstrap logs the divergence). `validate()` sees no kwargs, so an
    env-vs-env conflict is always a `config check` failure.
    """
    return FORWARD_AUTH_EXCLUSIVE_MESSAGE if chatgpt and anthropic else None


def _check_forward_auth_exclusive(values: Mapping[str, str | None], problems: list[str]) -> None:
    conflict = forward_auth_conflict(
        chatgpt=_as_flag(values.get("CORP_LLM_FORWARD_CHATGPT_AUTH")),
        anthropic=_as_flag(values.get("CORP_LLM_FORWARD_ANTHROPIC_AUTH")),
    )
    if conflict is not None:
        problems.append(conflict)


# Shared with bootstrap.build_guardrail(); the compose stacks the bridges ship on
# never run `validate()`, and their `env_file:` is a developer-owned file, so this
# has to be a boot-time refusal rather than a documented convention.
MASTER_KEY_VS_FORWARD_AUTH_MESSAGE = (
    "LITELLM_MASTER_KEY is set while a subscription-auth bridge is on "
    "(CORP_LLM_FORWARD_ANTHROPIC_AUTH / CORP_LLM_FORWARD_CHATGPT_AUTH). A master key makes "
    "litellm read the inbound Authorization header as one of its own virtual keys and answer "
    "401 before pre_call ever sees the developer's OAuth bearer, so the bridge can never run. "
    "A blank `LITELLM_MASTER_KEY=` counts as set: litellm keeps the empty value and enables "
    "proxy auth for anything that is not None. Remove the line entirely — a leftover "
    "LITELLM_MASTER_KEY in the local .env.demo that docker-compose passes in via `env_file:` "
    "is the usual source — or turn the bridge off"
)


def master_key_conflict(*, master_key: str | None, chatgpt: bool, anthropic: bool) -> str | None:
    """The one place the master-key-vs-bridge rule is expressed.

    Takes an already-resolved master key and already-parsed bridge flags so
    `build_guardrail()` can apply it to values that came from explicit kwargs.
    A master key on its own is legitimate (a plain BYOK-less litellm deploy uses
    one); only the combination is unserviceable.

    PRESENT, not truthy: litellm's `get_secret_str` hands `''` and `'   '` back
    unchanged and its proxy auth is skipped only for `master_key is None`, so a
    bare `LITELLM_MASTER_KEY=` still consumes the developer's bearer and 401s
    before pre_call. Anything other than absent is a conflict.
    """
    if not (chatgpt or anthropic):
        return None
    return MASTER_KEY_VS_FORWARD_AUTH_MESSAGE if master_key is not None else None


def _check_master_key_conflict(values: Mapping[str, str | None], problems: list[str]) -> None:
    conflict = master_key_conflict(
        master_key=values.get("LITELLM_MASTER_KEY"),
        chatgpt=_as_flag(values.get("CORP_LLM_FORWARD_CHATGPT_AUTH")),
        anthropic=_as_flag(values.get("CORP_LLM_FORWARD_ANTHROPIC_AUTH")),
    )
    if conflict is not None:
        problems.append(conflict)


def _check_no_op_sanitizer(values: Mapping[str, str | None], problems: list[str]) -> None:
    """Refuse to boot as a no-op sanitizer: oracle off requires the local-first floor.

    Gazetteer/rules alone are not a deterministic detection floor without either
    the local-first cascade (regex+checksum+NER) or the oracle backstop.
    """
    if _as_flag(values.get("CORP_LLM_ORACLE_ENABLED")):
        return
    if _as_flag(values.get("CORP_LLM_LOCAL_FIRST")):
        return
    problems.append(NO_OP_SANITIZER_MESSAGE)


ISSUANCE_TEAM_MAP_KEY = "CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"

_ISSUANCE_BOUNDS: tuple[tuple[str, str], ...] = (
    ("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", "token_ttl_days"),
    ("CORP_GATEWAY_ISSUE_MAX_ACTIVE", "max_active"),
    ("CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS", "min_interval_seconds"),
    ("CORP_GATEWAY_ISSUE_MAX_INFLIGHT", "max_inflight"),
    ("CORP_GATEWAY_ISSUE_RATE_PER_MINUTE", "rate_per_minute"),
)


@dataclass(frozen=True)
class IssuanceSettings:
    """Resolved developer-token issuance config. Built by :func:`issuance`."""

    issuer: str
    audience: str
    client_id: str
    jwks_url: str
    team_claim: str
    team_map: tuple[tuple[str, str], ...]
    user_claim: str
    operator_audience: str
    ca_bundle: str | None
    token_ttl_days: int
    max_active: int
    min_interval_seconds: int
    max_inflight: int
    rate_per_minute: int
    allow_insecure_http: bool = False


def _stripped(values: Mapping[str, str | None], name: str) -> str:
    return (values.get(name) or "").strip()


def _url_problem(name: str, url: str, *, prod: bool) -> str | None:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return f"{name}: must be an absolute http(s) URL"
    if prod and scheme != "https":
        return f"{name}: must be HTTPS when CORP_ENV is prod/production"
    return None


def _issuance_team_map(
    table: Mapping[str, object], problems: list[str]
) -> tuple[tuple[str, str], ...]:
    if not table:
        problems.append(
            f"{ISSUANCE_TEAM_MAP_KEY}: required when CORP_GATEWAY_ISSUE_OIDC_ISSUER is set — "
            "a non-empty group → team_id TOML table in the config file (env vars cannot "
            "carry tables)"
        )
        return ()
    pairs: list[tuple[str, str]] = []
    for group, team in table.items():
        if not group.strip() or not isinstance(team, str) or not team.strip():
            problems.append(
                f"{ISSUANCE_TEAM_MAP_KEY}: every entry must map a group to a non-empty "
                "team_id string"
            )
            return ()
        pairs.append((group, team.strip()))
    return tuple(pairs)


def _build_issuance(
    values: Mapping[str, str | None], table: Mapping[str, object], problems: list[str]
) -> IssuanceSettings | None:
    issuer = _stripped(values, "CORP_GATEWAY_ISSUE_OIDC_ISSUER").rstrip("/")
    if not issuer:
        return None
    start = len(problems)
    prod = _stripped(values, "CORP_ENV").lower() in ("prod", "production")
    required = {
        name: _stripped(values, name)
        for name in ("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID")
    }
    for name, value in required.items():
        if not value:
            problems.append(
                f"{name}: required when CORP_GATEWAY_ISSUE_OIDC_ISSUER is set. "
                f"{_BY_NAME[name].help}"
            )
    audience = required["CORP_GATEWAY_ISSUE_OIDC_AUDIENCE"]
    operator_audience = _stripped(values, "CORP_GATEWAY_OIDC_AUDIENCE")
    if audience and audience == operator_audience:
        problems.append(
            "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE must differ from CORP_GATEWAY_OIDC_AUDIENCE — "
            "an operator RBAC token must never mint developer tokens"
        )
    jwks_url = (
        _stripped(values, "CORP_GATEWAY_ISSUE_OIDC_JWKS_URL")
        or f"{issuer}/protocol/openid-connect/certs"
    )
    for name, url in (
        ("CORP_GATEWAY_ISSUE_OIDC_ISSUER", issuer),
        ("CORP_GATEWAY_ISSUE_OIDC_JWKS_URL", jwks_url),
    ):
        problem = _url_problem(name, url, prod=prod)
        if problem is not None:
            problems.append(problem)
    team_map = _issuance_team_map(table, problems)
    bounds: dict[str, int] = {}
    for name, field_name in _ISSUANCE_BOUNDS:
        raw = _stripped(values, name) or (_BY_NAME[name].default or "")
        try:
            bounds[field_name] = int(raw)
        except ValueError:
            problems.append(f"{name}={raw!r} is not an integer")
            continue
        if bounds[field_name] <= 0:
            problems.append(f"{name}: must be a positive integer")
    if len(problems) > start:
        return None
    return IssuanceSettings(
        issuer=issuer,
        audience=audience,
        client_id=required["CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID"],
        jwks_url=jwks_url,
        team_claim=_stripped(values, "CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM") or "groups",
        team_map=team_map,
        user_claim=_stripped(values, "CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM") or "preferred_username",
        operator_audience=operator_audience,
        ca_bundle=_stripped(values, "CORP_LLM_CA_BUNDLE") or None,
        allow_insecure_http=not prod,
        **bounds,
    )


ISSUANCE_NEEDS_POSTGRES = (
    "CORP_LLM_PG_DSN: required when CORP_GATEWAY_ISSUE_OIDC_ISSUER is set — developer token "
    "issuance serialises per subject across replicas in Postgres; the in-memory token store "
    "is for tests and the demo only"
)


def _serving_issuance(
    values: Mapping[str, str | None], problems: list[str]
) -> IssuanceSettings | None:
    result = _build_issuance(values, config.get_table(ISSUANCE_TEAM_MAP_KEY), problems)
    if _stripped(values, "CORP_GATEWAY_ISSUE_OIDC_ISSUER") and not _stripped(
        values, "CORP_LLM_PG_DSN"
    ):
        problems.append(ISSUANCE_NEEDS_POSTGRES)
        return None
    return result


def _check_issuance(values: Mapping[str, str | None], problems: list[str]) -> None:
    _serving_issuance(values, problems)


def issuance() -> IssuanceSettings | None:
    """Resolve issuance config: ``None`` when disabled, :class:`ConfigError` when unsafe."""
    problems: list[str] = []
    result = _build_issuance(_resolve(), config.get_table(ISSUANCE_TEAM_MAP_KEY), problems)
    if problems:
        raise ConfigError(problems)
    return result


def serving_issuance() -> IssuanceSettings | None:
    """:func:`issuance` plus what serving it needs (Postgres); the one resolver the
    entrypoint's boot check, ``build_health_router`` and ``config check`` share."""
    problems: list[str] = []
    result = _serving_issuance(_resolve(), problems)
    if problems:
        raise ConfigError(problems)
    return result


def _check_with_pydantic(values: Mapping[str, str | None], problems: list[str]) -> bool:
    """Validate required-endpoint + choices with pydantic. Returns False if absent.

    Fed ONLY the config-resolved values — never env/dotenv — so the resolution
    chain is preserved. Flags stay lenient (registry semantics), so this path
    never disagrees with the pydantic-absent path on a tested case.
    """
    try:
        import pydantic
    except ImportError:
        return False

    class _Model(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="ignore")

        # Keep these Literals in sync with AUTH_PROVIDERS / AUDIT_SINKS /
        # METRICS_EXPORTERS / TRACING_EXPORTERS.
        # Endpoint requiredness is conditional on the oracle switch (see
        # _check_oracle_endpoint below) — relaxed here so local mode can boot.
        CORP_LLM_ENDPOINT: str = ""
        CORP_LLM_AUTH_PROVIDER: Literal["noop", "bearer", "mtls", "oidc", "apikey"] = "noop"
        CORP_AUDIT_SINK: Literal["stdout", "langfuse", "list"] = "stdout"
        CORP_METRICS_EXPORTER: Literal["noop", "prometheus"] = "noop"
        CORP_TRACING_EXPORTER: Literal["noop"] = "noop"
        # Free-form optional keys — typed into the validated surface, no choice constraint.
        CORP_ENV: str = ""
        CORP_GATEWAY_OIDC_AUDIENCE: str = ""
        CORP_GATEWAY_OIDC_ISSUER: str = ""
        CORP_PROFILE_ROOT: str = ""

    payload: dict[str, str] = {k: v for k, v in values.items() if v is not None}
    for selector in (
        "CORP_LLM_AUTH_PROVIDER",
        "CORP_AUDIT_SINK",
        "CORP_METRICS_EXPORTER",
        "CORP_TRACING_EXPORTER",
    ):
        if selector in payload:
            payload[selector] = payload[selector].strip().lower()
    try:
        _Model.model_validate(payload)
    except pydantic.ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "config"
            problems.append(f"{loc}: {err['msg']}")
    return True


def validate() -> Settings:
    """Resolve + validate every key; raise :class:`ConfigError` on any problem.

    The startup fail-fast home (CLAUDE.md config-resolution contract). Endpoint
    and choice validation run through pydantic when it is importable, else the
    plain-Python fallback; conditional-credential and oversize checks always run.
    """
    values = _resolve()
    problems: list[str] = []
    if not _check_with_pydantic(values, problems):
        _check_required(values, problems)
        _check_choices(values, problems)
    _check_conditional(values, problems)
    _check_oversize(values, problems)
    _check_route_gate_extras(values, problems)
    _check_litellm_config(values, problems)
    check_serve_port(values, problems)
    _check_oracle_trigger(values, problems)
    _check_oracle_endpoint(values, problems)
    _check_corp_ner_endpoint(values, problems)
    _check_no_op_sanitizer(values, problems)
    _check_forward_auth_exclusive(values, problems)
    _check_master_key_conflict(values, problems)
    _check_issuance(values, problems)
    if problems:
        raise ConfigError(list(dict.fromkeys(problems)))
    return Settings(values=values)
