"""gateway-admin — operator CLI for the corp LLM gateway.

This is a skeleton (M2-5). Subcommands print the action that would be taken;
the actual Postgres-facing implementations land alongside M2-1..M2-4.
"""

import argparse
import sys
from collections.abc import Sequence


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

    team_set_retention = team_sub.add_parser("set-retention", help="set retention overrides for a team")
    team_set_retention.add_argument("--team-id", required=True)
    team_set_retention.add_argument("--hot-days", type=int, default=90)
    team_set_retention.add_argument("--cold-years", type=int, default=7)

    p_token = sub.add_parser("token", help="manage corp tokens")
    token_sub = p_token.add_subparsers(dest="token_command", required=True)

    token_revoke = token_sub.add_parser("revoke", help="revoke a corp token")
    token_revoke.add_argument("--user", required=True)

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

    parser.error("unknown command path")
    return 2


if __name__ == "__main__":
    sys.exit(main())
