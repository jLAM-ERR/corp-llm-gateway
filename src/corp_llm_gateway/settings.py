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
``CORP_LLM_ENDPOINT``, which otherwise silently defaults to a non-routable
placeholder and only surfaces as a 503 on the first gazetteer-hit request
(``bootstrap.py`` only warns).

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
        required=True,
        help="corp vLLM base URL (…/v1); required — no routable default",
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
    Key("CORP_LLM_LOG_LEVEL", default="INFO", help="log level"),
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
    # ── Test-data allowlist (sanitizer/allowlist.py) ─────────────────────────
    Key("CORP_LLM_TESTDATA_ALLOWLIST", default="", help="inline never-redact test values"),
    Key("CORP_LLM_TESTDATA_ALLOWLIST_FILE", default="", help="never-redact test values file"),
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


def _check_oracle_trigger(values: Mapping[str, str | None], problems: list[str]) -> None:
    from corp_llm_gateway.sanitizer.orchestrator import normalize_oracle_trigger

    try:
        normalize_oracle_trigger(values.get("CORP_LLM_ORACLE_TRIGGER"))
    except ValueError as exc:
        problems.append(f"CORP_LLM_ORACLE_TRIGGER: {exc}")


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
        CORP_LLM_ENDPOINT: str
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
    _check_oracle_trigger(values, problems)
    if problems:
        raise ConfigError(list(dict.fromkeys(problems)))
    return Settings(values=values)
