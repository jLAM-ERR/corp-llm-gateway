"""Seal shipped profile bundles with their content hash (D6 helper).

The D5 default bundles under ``profiles/defaults/`` must declare a ``content_hash``
so D6's fail-closed integrity check is live for them — otherwise a tampered term
file egresses undetected because ``verify_integrity`` only runs when the manifest
declares a hash. ``seal_profile`` recomputes the order-independent bundle hash
(``content_hash_for_dir``) and writes it into the bundle's ``profile.toml``.

Run after editing a shipped bundle::

    python -m corp_llm_gateway.profiles.seal src/corp_llm_gateway/profiles/defaults

``tests/profiles/test_default_bundles_sealed.py`` guards against a bundle shipped
unsealed or a term file edited without re-sealing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from corp_llm_gateway.profiles.file_loader import content_hash_for_dir
from corp_llm_gateway.profiles.lint import discover_profiles

_HASH_KEY = re.compile(r"^\s*content_hash\s*=")
_NAME_KEY = re.compile(r"^\s*name\s*=")


def seal_profile(profile_dir: Path) -> str:
    """Compute the bundle hash and write it into ``profile.toml``; return the hash.

    Drops any stale ``content_hash`` line and re-inserts the fresh one right after
    the top-level ``name`` key so it stays outside any ``[table]`` section.
    """
    manifest_path = profile_dir / "profile.toml"
    digest = content_hash_for_dir(profile_dir)
    out: list[str] = []
    inserted = False
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if _HASH_KEY.match(line):
            continue
        out.append(line)
        if not inserted and _NAME_KEY.match(line):
            out.append(f'content_hash = "{digest}"')
            inserted = True
    if not inserted:
        out.insert(0, f'content_hash = "{digest}"')
    manifest_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return digest


def seal_root(root: Path) -> dict[str, str]:
    """Seal every discovered profile under ``root``; return ``{profile_id: hash}``."""
    return {profile_id: seal_profile(root / profile_id) for profile_id in discover_profiles(root)}


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m corp_llm_gateway.profiles.seal <profiles-root>", file=sys.stderr)
        return 2
    for profile_id, digest in seal_root(Path(argv[1])).items():
        print(f"{profile_id}: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
