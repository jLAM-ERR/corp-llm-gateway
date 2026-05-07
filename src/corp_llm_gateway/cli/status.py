"""corp-llm-gateway status — onboarded-laptop diagnostics (M6-5)."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_TOKEN_FILE = "~/.corp-llm-gateway/token"
DEFAULT_VERSION_FILE = "~/.corp-llm-gateway/VERSION"
DEFAULT_GATEWAY_URL = "https://gateway.corp.lan"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corp-llm-gateway", description="Gateway client status")
    parser.add_argument("--gateway-url", default=os.environ.get("CORP_GATEWAY_URL", DEFAULT_GATEWAY_URL))
    parser.add_argument("--token-file", default=os.environ.get("CORP_GATEWAY_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    parser.add_argument("--version-file", default=DEFAULT_VERSION_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show client status (default)")
    args = parser.parse_args(argv)

    info = _gather_status(
        gateway_url=args.gateway_url,
        token_file=Path(args.token_file).expanduser(),
        version_file=Path(args.version_file).expanduser(),
    )

    if args.json:
        print(json.dumps(info, indent=2))
        return 0 if info["healthy"] else 1

    _print_human(info)
    return 0 if info["healthy"] else 1


def _gather_status(
    *,
    gateway_url: str,
    token_file: Path,
    version_file: Path,
) -> dict[str, object]:
    info: dict[str, object] = {
        "gateway_url": gateway_url,
        "token_file": str(token_file),
        "token_present": False,
        "version": None,
        "live": False,
        "healthy": False,
    }

    if token_file.is_file():
        info["token_present"] = True
        try:
            stat = token_file.stat()
            info["token_age_seconds"] = int(
                (datetime.now(UTC).timestamp() - stat.st_mtime)
            )
        except OSError:
            pass

    if version_file.is_file():
        try:
            info["version"] = version_file.read_text().strip()
        except OSError:
            pass

    info["live"] = _probe_live(gateway_url)
    info["healthy"] = bool(info["token_present"] and info["live"])
    return info


def _probe_live(gateway_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{gateway_url}/healthz/live", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _print_human(info: dict[str, object]) -> None:
    out = sys.stdout
    out.write(f"gateway_url:    {info['gateway_url']}\n")
    out.write(f"live:           {'yes' if info['live'] else 'no'}\n")
    out.write(f"token_present:  {'yes' if info['token_present'] else 'no'}\n")
    if "token_age_seconds" in info:
        days = int(info["token_age_seconds"]) // 86400
        out.write(f"token_age:      {days}d\n")
    out.write(f"version:        {info.get('version') or 'unknown'}\n")
    if info["healthy"]:
        out.write("\n✓ healthy\n")
    else:
        out.write("\n✗ unhealthy — run install.sh\n")


if __name__ == "__main__":
    sys.exit(main())
