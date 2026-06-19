"""gateway-admin — operator CLI for the corp LLM gateway.

The ``team`` / ``token`` subcommands are still skeletons (M2-5): they print
the action that would be taken; the Postgres-facing implementations land
alongside M2-1..M2-4. The ``sanitize`` subcommand is a real implementation —
it runs the live three-tier sanitizer against the corp LLM and prints the
before/after redaction (useful for the demo walkthrough).
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from corp_llm_gateway import config
from corp_llm_gateway.auth import BearerAuthProvider, NoopAuthProvider
from corp_llm_gateway.corp_llm import CorpLlmClient, CorpLlmHttpError
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
    ssl_verify = config.get("SSL_VERIFY", "true").lower() != "false"
    # Let CorpLlmClient OWN its http client so `_run`'s `aclose()` actually
    # closes the connection pool — a one-shot CLI must not leak it. `verify`
    # carries the SSL_VERIFY opt-out the demo uses for the corp internal CA.
    corp_llm = CorpLlmClient(
        base_url=root,
        model=model,
        auth_provider=auth_provider,
        timeout=30.0,
        verify=ssl_verify,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-admin")
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
        print(f"token.revoke user={args.user}")
        return 0

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
