"""Static lint for profile bundles (D8).

``lint_bundle`` checks ONE profile layer: its ``profile.toml`` parses, every
detector name it lists exists in ``DETECTOR_REGISTRY``, its term files +
``replace.md`` parse, and its ``extends`` chain resolves with no cycle and within
``max_depth``. ``lint_root`` discovers every profile dir under a root and lints
each. A malformed manifest, an unknown detector name, or a cyclic/too-deep
``extends`` chain raises ``BundleLintError`` — the data-lane gate (CODEOWNERS:
``profiles/**`` is compliance-owned) so a bad bundle fails at lint, not at egress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corp_llm_gateway.profiles.base import ProfileNotFoundError, ProfileParseError
from corp_llm_gateway.profiles.file_loader import FileProfileLoader
from corp_llm_gateway.profiles.manifest import (
    MAX_EXTENDS_DEPTH,
    ProfileCycleError,
    ProfileDepthError,
    ProfileIntegrityError,
    ProfileManifest,
    resolve_extends,
)
from corp_llm_gateway.profiles.registry import DETECTOR_REGISTRY
from corp_llm_gateway.rules.loader import RulesParseError

if TYPE_CHECKING:
    from pathlib import Path


class BundleLintError(Exception):
    pass


def discover_profiles(root: Path) -> tuple[str, ...]:
    """Every immediate subdir of ``root`` that carries a ``profile.toml``."""
    if not root.is_dir():
        raise BundleLintError(f"profiles root does not exist: {root}")
    return tuple(
        child.name
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / "profile.toml").is_file()
    )


async def lint_bundle(
    root: Path, profile_id: str, *, max_depth: int = MAX_EXTENDS_DEPTH
) -> ProfileManifest:
    """Lint one profile layer; return its manifest or raise ``BundleLintError``."""
    loader = FileProfileLoader(root)
    try:
        source = await loader.read_source(profile_id)
    except (
        ProfileParseError,
        ProfileNotFoundError,
        ProfileIntegrityError,
        RulesParseError,
    ) as exc:
        raise BundleLintError(f"{profile_id}: {exc}") from exc

    unknown = tuple(name for name in source.manifest.detectors if name not in DETECTOR_REGISTRY)
    if unknown:
        known = tuple(sorted(DETECTOR_REGISTRY))
        raise BundleLintError(
            f"{profile_id}: unknown detector(s) {unknown}; expected one of {known}"
        )

    try:
        await resolve_extends([profile_id], loader.read_manifest, max_depth=max_depth)
    except (
        ProfileCycleError,
        ProfileDepthError,
        ProfileParseError,
        ProfileNotFoundError,
    ) as exc:
        raise BundleLintError(f"{profile_id}: extends does not resolve: {exc}") from exc

    return source.manifest


async def lint_root(root: Path, *, max_depth: int = MAX_EXTENDS_DEPTH) -> tuple[str, ...]:
    """Lint every discovered profile under ``root``; return the linted ids."""
    ids = discover_profiles(root)
    for profile_id in ids:
        await lint_bundle(root, profile_id, max_depth=max_depth)
    return ids
