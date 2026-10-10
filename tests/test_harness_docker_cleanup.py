"""Every container the test harness removes takes its anonymous volumes with it.

`docker rm -f` without `-v` leaves each container's anonymous volumes behind (a
Postgres image declares one for its data directory), and a few hundred test runs
filled the Docker disk. A container started with `--rm` is covered by `--rm`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("tests", "scripts")
# Not the harness: removes the stack's one-shot service containers on the deploy host.
EXCLUDED = frozenset({"scripts/deploy/deploy.sh"})

# `"rm", "-f"` as call arguments, or `docker rm -f` in a shell line, with no `-v`
# among the flags that follow.
_PY_RM = re.compile(r'"rm"((?:\s*,\s*"-[a-z]+")+)')
_SH_RM = re.compile(r"\bdocker\s+rm((?:\s+-[a-z]+)+)")


def _offenders() -> list[str]:
    found = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            rel = path.relative_to(ROOT).as_posix()
            if path.suffix not in (".py", ".sh") or rel in EXCLUDED:
                continue
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in (_PY_RM, _SH_RM):
                for match in pattern.finditer(text):
                    flags = re.findall(r"-[a-z]+", match.group(1))
                    if not any("v" in flag for flag in flags):
                        line = text.count("\n", 0, match.start()) + 1
                        found.append(f"{rel}:{line}: {match.group(0)}")
    return found


def test_every_docker_rm_in_the_harness_removes_anonymous_volumes() -> None:
    assert _offenders() == []


def test_the_scan_sees_a_removal_without_volumes() -> None:
    assert _PY_RM.search('docker("rm", "-f", name)')
    assert _SH_RM.search("docker rm -f corp-x")
    flags = re.findall(r"-[a-z]+", _PY_RM.search('docker("rm", "-f", "-v", name)').group(1))
    assert "-v" in flags
