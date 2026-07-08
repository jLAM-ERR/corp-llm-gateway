"""gateway-admin — operator CLI for the corp LLM gateway.

The ``team`` / ``token`` subcommands are still skeletons (M2-5): they print
the action that would be taken; the Postgres-facing implementations land
alongside M2-1..M2-4. The ``sanitize`` subcommand is a real implementation —
it runs the live three-tier sanitizer against the corp LLM and prints the
before/after redaction (useful for the demo walkthrough).
"""

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence
from typing import Any, cast, get_args

from corp_llm_gateway import config, extensions, providers
from corp_llm_gateway.audit import get_sink, register_sink, sink_name_for
from corp_llm_gateway.auth import BearerAuthProvider, NoopAuthProvider
from corp_llm_gateway.auth.rbac import OperatorDenied, get_admin_token, verify_operator
from corp_llm_gateway.corp_llm import CorpLlmClient, CorpLlmHttpError
from corp_llm_gateway.extensions import ExtensionKind, ExtensionRegistry, ExtensionSpec
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator, SanitizeResult
from corp_llm_gateway.sanitizer.engine import AllStrategiesFailedError
from corp_llm_gateway.storage import InMemoryMappingStore


class _NoTeamRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _build_orchestrator(model: str) -> tuple[SanitizationOrchestrator, CorpLlmClient]:
    corp_endpoint = config.get_required("CORP_LLM_ENDPOINT")
    root = corp_endpoint.rstrip("/").removesuffix("/v1")
    token = config.get("CORP_LLM_AUTH_TOKEN", "")
    auth_provider = BearerAuthProvider(token=token) if token else NoopAuthProvider()
    # Let CorpLlmClient OWN its http client so `_run`'s `aclose()` actually
    # closes the connection pool. `verify` uses CORP_LLM_CA_BUNDLE (corp
    # internal CA) when set, else the SSL_VERIFY bool.
    corp_llm = CorpLlmClient(
        base_url=root,
        model=model,
        auth_provider=auth_provider,
        timeout=30.0,
        verify=config.corp_llm_verify(),
    )
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _NoTeamRules())
    return orch, corp_llm


async def _run(
    orch: SanitizationOrchestrator,
    corp_llm: CorpLlmClient,
    text: str,
    team_id: str,
) -> SanitizeResult:
    try:
        return await orch.sanitize(text, team_id=team_id, conversation_id="gateway-admin-cli")
    finally:
        await corp_llm.aclose()


def _enforce_rbac(args: argparse.Namespace) -> int | None:
    """Check gateway:operator claim before a mutating command.

    Returns 2 on denial (and prints to stderr), None when the caller is allowed.
    Skipped entirely when CORP_GATEWAY_RBAC=0 (local dev bypass).
    """
    if config.get("CORP_GATEWAY_RBAC", "1") == "0":
        return None
    token = get_admin_token(getattr(args, "token", None))
    try:
        verify_operator(token)
    except OperatorDenied:
        print("error: gateway:operator role required", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return None


def _populate_registry(registry: ExtensionRegistry) -> None:
    """Register the same extensions a running gateway exposes, so the read verbs
    reflect the real set (never a misleadingly-empty list).

    In a `gateway-admin` process nothing calls ``build_guardrail``, so the shared
    extension registry starts empty. Two honest sources fill it: providers, which
    register into their own v1-guarded ProviderRegistry at import (bridged here),
    and the configured audit sink, built via the audit factory exactly as
    ``bootstrap.build_guardrail`` does. A misconfigured optional sink (e.g.
    langfuse with no URL/keys) must not crash a read-only verb, so its
    registration is best-effort.
    """
    for name in providers.REGISTRY.known():
        spec = providers.REGISTRY.spec(name)
        registry.register(spec, lambda n=name: providers.REGISTRY.get(n), replace=True)
    with contextlib.suppress(RuntimeError, ValueError):
        sink = get_sink()
        register_sink(registry, sink, sink_name_for(sink))


def _all_specs(registry: ExtensionRegistry, kind: str | None = None) -> list[ExtensionSpec]:
    kinds: tuple[Any, ...] = (kind,) if kind else get_args(ExtensionKind)
    specs: list[ExtensionSpec] = []
    for k in kinds:
        specs.extend(ext.spec for ext in registry.enabled(cast(ExtensionKind, k)))
    return sorted(specs, key=lambda s: (s.kind, s.name))


def _parse_ref(ref: str) -> tuple[str, str] | None:
    kind, sep, name = ref.partition(":")
    if not sep or not kind or not name:
        return None
    return kind, name


def _spec_to_dict(spec: ExtensionSpec) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": spec.kind,
        "name": spec.name,
        "version": spec.version,
        "api_version": spec.api_version,
        "fail_policy": spec.fail_policy,
        "capabilities": sorted(spec.capabilities),
    }
    # ProviderSpec adds role/wire_format/health_url; surface them when present.
    for extra in ("role", "wire_format", "health_url"):
        if hasattr(spec, extra):
            data[extra] = getattr(spec, extra)
    return data


def _print_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _ext_list(registry: ExtensionRegistry, *, kind: str | None, as_json: bool) -> int:
    specs = _all_specs(registry, kind)
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "kind": s.kind,
                        "name": s.name,
                        "version": s.version,
                        "api_version": s.api_version,
                        "fail_policy": s.fail_policy,
                    }
                    for s in specs
                ]
            )
        )
        return 0
    if not specs:
        print("no extensions registered")
        return 0
    rows: list[tuple[str, ...]] = [("KIND", "NAME", "VERSION", "API_VERSION", "FAIL-POLICY")]
    rows += [(s.kind, s.name, s.version, s.api_version, s.fail_policy) for s in specs]
    _print_table(rows)
    return 0


def _ext_inspect(registry: ExtensionRegistry, ref: str, *, as_json: bool) -> int:
    parsed = _parse_ref(ref)
    if parsed is None:
        print(f"error: expected KIND:NAME, got {ref!r}", file=sys.stderr)
        return 2
    kind, name = parsed
    try:
        ext = registry.get(cast(ExtensionKind, kind), name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    data = _spec_to_dict(ext.spec)
    if as_json:
        print(json.dumps(data))
        return 0
    for key, value in data.items():
        if isinstance(value, list):
            rendered = ", ".join(value) if value else "-"
        else:
            rendered = "-" if value is None else str(value)
        print(f"{key}: {rendered}")
    return 0


def _ext_health(registry: ExtensionRegistry, *, as_json: bool) -> int:
    report = asyncio.run(registry.health_all())
    policies = {f"{s.kind}:{s.name}": s.fail_policy for s in _all_specs(registry)}
    unhealthy_fail_closed = [
        key
        for key, status in report.items()
        if not status.healthy and policies.get(key) == "fail-closed"
    ]
    if as_json:
        print(
            json.dumps(
                {
                    "extensions": [
                        {
                            "extension": key,
                            "healthy": status.healthy,
                            "detail": status.detail,
                            "fail_policy": policies.get(key, "unknown"),
                        }
                        for key, status in sorted(report.items())
                    ],
                    "healthy": not unhealthy_fail_closed,
                }
            )
        )
    elif not report:
        print("no extensions registered")
    else:
        rows: list[tuple[str, ...]] = [("EXTENSION", "HEALTH", "FAIL-POLICY", "DETAIL")]
        rows += [
            (
                key,
                "OK" if status.healthy else "UNHEALTHY",
                policies.get(key, "unknown"),
                status.detail,
            )
            for key, status in sorted(report.items())
        ]
        _print_table(rows)
    return 1 if unhealthy_fail_closed else 0


def _require_known(registry: ExtensionRegistry, ref: str) -> int | None:
    """None when ``ref`` names a registered extension, else prints + returns 2."""
    parsed = _parse_ref(ref)
    if parsed is None:
        print(f"error: expected KIND:NAME, got {ref!r}", file=sys.stderr)
        return 2
    kind, name = parsed
    try:
        registry.get(cast(ExtensionKind, kind), name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return None


def _ext_enable(registry: ExtensionRegistry, ref: str, *, team: str | None, rollout: str) -> int:
    rc = _require_known(registry, ref)
    if rc is not None:
        return rc
    # ADR-001 stub: the target exists, but there is no per-extension state store
    # to persist enable/disable (TeamConfig has no such field). Do not fake it.
    raise NotImplementedError(
        f"extensions enable {ref} (team={team}, rollout={rollout}): "
        "needs an extension-state store — follow-up"
    )


def _ext_disable(registry: ExtensionRegistry, ref: str, *, team: str | None) -> int:
    rc = _require_known(registry, ref)
    if rc is not None:
        return rc
    raise NotImplementedError(
        f"extensions disable {ref} (team={team}): needs an extension-state store — follow-up"
    )


def _dispatch_extensions(args: argparse.Namespace) -> int:
    if args.ext_command in ("enable", "disable"):
        rbac_rc = _enforce_rbac(args)
        if rbac_rc is not None:
            return rbac_rc
    registry = extensions.REGISTRY
    _populate_registry(registry)
    if args.ext_command == "list":
        return _ext_list(registry, kind=args.kind, as_json=args.json_output)
    if args.ext_command == "inspect":
        return _ext_inspect(registry, args.ref, as_json=args.json_output)
    if args.ext_command == "health":
        return _ext_health(registry, as_json=args.json_output)
    if args.ext_command == "enable":
        return _ext_enable(registry, args.ref, team=args.team, rollout=args.rollout)
    return _ext_disable(registry, args.ref, team=args.team)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-admin")
    parser.add_argument(
        "--token",
        default=None,
        metavar="JWT",
        help="operator JWT (or set CORP_GATEWAY_ADMIN_TOKEN)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_team = sub.add_parser("team", help="manage teams")
    team_sub = p_team.add_subparsers(dest="team_command", required=True)

    team_create = team_sub.add_parser("create", help="create a team")
    team_create.add_argument("--team-id", required=True)
    team_create.add_argument("--name", required=True)

    team_set_rules = team_sub.add_parser("set-rules", help="set replace.md for a team")
    team_set_rules.add_argument("--team-id", required=True)
    team_set_rules.add_argument("--from-file", required=True)

    team_set_retention = team_sub.add_parser(
        "set-retention", help="set retention overrides for a team"
    )
    team_set_retention.add_argument("--team-id", required=True)
    team_set_retention.add_argument("--hot-days", type=int, default=90)
    team_set_retention.add_argument("--cold-years", type=int, default=7)

    p_token = sub.add_parser("token", help="manage corp tokens")
    token_sub = p_token.add_subparsers(dest="token_command", required=True)

    token_revoke = token_sub.add_parser("revoke", help="revoke a corp token")
    token_revoke.add_argument("--user", required=True)

    p_ext = sub.add_parser("extensions", help="inspect and manage registered extensions")
    ext_sub = p_ext.add_subparsers(dest="ext_command", required=True)

    ext_list = ext_sub.add_parser("list", help="list registered extensions")
    ext_list.add_argument("--kind", choices=list(get_args(ExtensionKind)), default=None)
    ext_list.add_argument("--json", dest="json_output", action="store_true")

    ext_inspect = ext_sub.add_parser("inspect", help="show one extension's full spec")
    ext_inspect.add_argument("ref", metavar="KIND:NAME")
    ext_inspect.add_argument("--json", dest="json_output", action="store_true")

    ext_health = ext_sub.add_parser(
        "health", help="probe extension health (nonzero if a fail-closed ext is unhealthy)"
    )
    ext_health.add_argument("--json", dest="json_output", action="store_true")

    ext_enable = ext_sub.add_parser("enable", help="enable an extension (RBAC-gated)")
    ext_enable.add_argument("ref", metavar="KIND:NAME")
    ext_enable.add_argument("--team", default=None)
    ext_enable.add_argument("--rollout", choices=["off", "canary", "on"], default="on")

    ext_disable = ext_sub.add_parser("disable", help="disable an extension (RBAC-gated)")
    ext_disable.add_argument("ref", metavar="KIND:NAME")
    ext_disable.add_argument("--team", default=None)

    p_sanitize = sub.add_parser("sanitize", help="show BEFORE/AFTER sanitization for a prompt")
    p_sanitize.add_argument("text", help="prompt text to sanitize")
    p_sanitize.add_argument("--team-id", default="default")
    p_sanitize.add_argument(
        "--model",
        default=config.get("CORP_LLM_MODEL", "GLM-5.1-AWQ"),
    )
    p_sanitize.add_argument("--json", dest="json_output", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "team":
        rbac_rc = _enforce_rbac(args)
        if rbac_rc is not None:
            return rbac_rc
        if args.team_command == "create":
            print(f"team.create team_id={args.team_id} name={args.name}")
            return 0
        if args.team_command == "set-rules":
            print(f"team.set_rules team_id={args.team_id} from_file={args.from_file}")
            return 0
        if args.team_command == "set-retention":
            print(
                f"team.set_retention team_id={args.team_id} "
                f"hot_days={args.hot_days} cold_years={args.cold_years}"
            )
            return 0

    if args.command == "token" and args.token_command == "revoke":
        rbac_rc = _enforce_rbac(args)
        if rbac_rc is not None:
            return rbac_rc
        print(f"token.revoke user={args.user}")
        return 0

    if args.command == "extensions":
        return _dispatch_extensions(args)

    if args.command == "sanitize":
        try:
            orch, corp_llm = _build_orchestrator(args.model)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        try:
            result = asyncio.run(_run(orch, corp_llm, args.text, args.team_id))
        except (CorpLlmHttpError, AllStrategiesFailedError) as exc:
            print(
                f"corp sanitization LLM unavailable: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1

        if args.json_output:
            print(
                json.dumps(
                    {
                        "before": args.text,
                        "after": result.sanitized_text,
                        "redaction_count": len(result.pairs),
                        "pairs": [[o, r] for o, r in result.pairs],
                        "cache_a_hit": result.cache_a_hit,
                        "skipped": result.skipped,
                    }
                )
            )
        else:
            print(f"BEFORE: {args.text}")
            print(f"AFTER : {result.sanitized_text}")
            if result.skipped:
                # Oversize payload (M1-11): the pre-pass was bypassed, so the
                # content went UNREDACTED. Say so explicitly — "redactions: 0"
                # alone would read as a false "clean input".
                print("redactions: SKIPPED — payload over size threshold; content sent UNREDACTED")
            else:
                print(f"redactions: {len(result.pairs)}")
                for original, replacement in result.pairs:
                    print(f"  {original} -> {replacement}")
        return 0

    parser.error("unknown command path")
    return 2


if __name__ == "__main__":
    sys.exit(main())
