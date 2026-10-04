"""Environment fingerprint of a test venv: which optional stacks are importable and the
exact ``name==version`` set, with the editable checkout's path normalised away.

``python -m tests._gates.fingerprint <env> --print|--write|--check``
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import re
import sys
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "tests" / "_manifests"

# Present in full, absent in minimal: the import names the suite's skips key on.
MARKERS = ("litellm", "prometheus_client", "asyncpg", "natasha", "spacy", "cryptography")
EXPECTED_PRESENT = {"minimal": False, "full": True}
# The venv bootstrap tools follow the interpreter, not the recipe.
BOOTSTRAP = {"pip", "setuptools", "wheel"}


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _editable(dist: importlib.metadata.Distribution) -> bool:
    text = dist.read_text("direct_url.json")
    if not text:
        return False
    return bool(json.loads(text).get("dir_info", {}).get("editable"))


def packages() -> list[str]:
    seen: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = _normalise(dist.metadata["Name"])
        if name in BOOTSTRAP:
            continue
        entry = f"{name}=={dist.version}"
        if _editable(dist):
            entry += " (editable <checkout>)"
        seen[name] = entry
    return sorted(seen.values())


def current() -> dict[str, Any]:
    markers = {name: importlib.util.find_spec(name) is not None for name in MARKERS}
    litellm = None
    if markers["litellm"]:
        litellm = importlib.metadata.version("litellm")
    return {
        "python": "{}.{}".format(*sys.version_info[:2]),
        "markers": markers,
        "litellm_version": litellm,
        "packages": packages(),
    }


@cache
def litellm_pin() -> str:
    """The exact litellm version pyproject pins (tests/test_litellm_pin.py holds every other site
    to it; the full constraints file is held here, in problems())."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for requirement in project["dependencies"]:
        match = re.fullmatch(r"litellm==(\S+)", requirement.strip())
        if match:
            return match.group(1)
    raise SystemExit("pyproject.toml pins no litellm==<version>")


def constraints_path(env: str) -> Path:
    return ROOT / "scripts" / f"test-env.{env}.txt"


def constraints_litellm(env: str) -> str | None:
    """The ``litellm==<version>`` the environment's constraints file installs, if any."""
    match = re.search(r"^litellm==(\S+)\s*$", constraints_path(env).read_text(), re.MULTILINE)
    return match.group(1) if match else None


def path_for(env: str) -> Path:
    return MANIFESTS / f"env_fingerprint.{env}.json"


def problems(env: str, actual: dict[str, Any], recorded: dict[str, Any]) -> list[str]:
    found = []
    want = EXPECTED_PRESENT[env]
    for name, present in actual["markers"].items():
        if present is not want:
            found.append(f"{name} is {'present' if present else 'absent'} in the {env} env")
    if env == "full" and actual["litellm_version"] != litellm_pin():
        found.append(f"litellm is {actual['litellm_version']}, not {litellm_pin()}")
    if env == "full" and constraints_litellm(env) != litellm_pin():
        found.append(
            f"{constraints_path(env).relative_to(ROOT)} pins litellm=={constraints_litellm(env)},"
            f" pyproject.toml pins {litellm_pin()}"
        )
    for key in ("python", "markers", "litellm_version"):
        if actual[key] != recorded[key]:
            found.append(f"{key}: recorded {recorded[key]!r}, now {actual[key]!r}")
    extra = sorted(set(actual["packages"]) - set(recorded["packages"]))
    missing = sorted(set(recorded["packages"]) - set(actual["packages"]))
    found += [f"not in the recorded set: {entry}" for entry in extra]
    found += [f"recorded but not installed: {entry}" for entry in missing]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("env", choices=sorted(EXPECTED_PRESENT))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    actual = current()
    if args.print:
        print(json.dumps({k: v for k, v in actual.items() if k != "packages"}, sort_keys=True))
        return 0
    if args.write:
        path_for(args.env).write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
        return 0
    recorded = json.loads(path_for(args.env).read_text())
    found = problems(args.env, actual, recorded)
    for line in found:
        print(f"FINGERPRINT {args.env}: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
