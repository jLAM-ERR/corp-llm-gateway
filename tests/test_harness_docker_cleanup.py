"""Every container the test harness removes takes its anonymous volumes with it.

`docker rm -f` without `-v` leaves each container's anonymous volumes behind (a
Postgres image declares one for its data directory), and a few hundred test runs
filled the Docker disk. A container started with `--rm` is covered by `--rm`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("tests", "scripts")
# Not the harness: removes the stack's one-shot service containers on the deploy host.
EXCLUDED = frozenset({"scripts/deploy/deploy.sh"})

# A `docker rm` / `docker container rm` as call arguments (either quote style, any
# helper whose name starts with `docker`, or a `["docker", ...]` argv) or as a
# shell line, with the flags that follow it.
_Q = r"[\"']"
_PY_RM = re.compile(
    rf"(?:docker\w*\(\s*|{_Q}docker{_Q}\s*,\s*)(?:{_Q}container{_Q}\s*,\s*)?{_Q}rm{_Q}"
    rf"((?:\s*,\s*{_Q}-{{1,2}}[a-z-]+{_Q})*)"
)
_SH_RM = re.compile(r"\bdocker\s+(?:container\s+)?rm\b((?:\s+-{1,2}[a-z-]+)*)")


def _removes_volumes(flags: str) -> bool:
    found = re.findall(r"-{1,2}[a-z-]+", flags)
    return any(f == "--volumes" or (not f.startswith("--") and "v" in f) for f in found)


def _rm_sites() -> tuple[int, list[str]]:
    sites = 0
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
                    sites += 1
                    if not _removes_volumes(match.group(1)):
                        line = text.count("\n", 0, match.start()) + 1
                        found.append(f"{rel}:{line}: {match.group(0)}")
    return sites, found


def test_every_docker_rm_in_the_harness_removes_anonymous_volumes() -> None:
    sites, offenders = _rm_sites()

    assert sites >= 7, f"the scan found only {sites} docker rm sites; it no longer sees them"
    assert offenders == []


@pytest.mark.parametrize(
    "line",
    [
        'docker("rm", name)',
        'docker("rm", "-f", name)',
        "docker('rm', '-f', name)",
        'docker("rm", "--force", name)',
        '_docker("container", "rm", "-f", name)',
        '["docker", "rm", "-f", name]',
        'docker rm "$c"',
        "docker rm -f corp-x",
        "docker container rm --force corp-x",
    ],
)
def test_the_scan_sees_a_removal_without_volumes(line: str) -> None:
    match = _PY_RM.search(line) or _SH_RM.search(line)

    assert match is not None
    assert not _removes_volumes(match.group(1))


@pytest.mark.parametrize(
    "line",
    ['docker("rm", "-f", "-v", name)', "docker('rm', '-fv', n)", "docker rm --force --volumes x"],
)
def test_the_scan_accepts_a_removal_with_volumes(line: str) -> None:
    match = _PY_RM.search(line) or _SH_RM.search(line)

    assert match is not None
    assert _removes_volumes(match.group(1))
