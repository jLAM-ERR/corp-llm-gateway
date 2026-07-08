"""Filesystem profile loader (near-verbatim mirror of rules/file_loader.py).

A profile lives at ``<root_dir>/<profile_id>/`` and holds ``profile.toml`` plus
optional ``replace.md``, term files (``products.txt`` / ``regulated.txt`` /
``markings.txt``), and ``allowlist.txt``. ``read_layer_source`` reads ONE layer's
source; ``build_bundle`` merges an ordered ``[core, ..., most-specific]`` layer
list into a single ``ProfileBundle`` (monotone-tightening — composition only adds
redaction). ``FileProfileLoader.load`` is the single-layer case.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from corp_llm_gateway.profiles.base import (
    PolicyKnobs,
    ProfileBundle,
    ProfileLoader,
    ProfileNotFoundError,
)
from corp_llm_gateway.profiles.manifest import (
    ProfileManifest,
    compute_content_hash,
    parse_manifest,
    verify_integrity,
)
from corp_llm_gateway.profiles.registry import build_detectors
from corp_llm_gateway.rules.gazetteer import Gazetteer, load_terms
from corp_llm_gateway.rules.models import Rules
from corp_llm_gateway.rules.parser import parse
from corp_llm_gateway.sanitizer.allowlist import Allowlist

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any


@dataclass(frozen=True)
class LayerSource:
    """One profile layer's source data, kept mergeable across layers."""

    profile_id: str
    manifest: ProfileManifest
    term_categories: dict[str, str]
    rules: Rules
    allowlist_originals: tuple[str, ...]


class FileProfileLoader(ProfileLoader):
    def __init__(self, root_dir: Path) -> None:
        self._root = Path(root_dir)

    @property
    def root(self) -> Path:
        return self._root

    async def load(self, profile_id: str) -> ProfileBundle:
        source = await self.read_source(profile_id)
        return build_bundle([source])

    async def read_source(self, profile_id: str) -> LayerSource:
        return await asyncio.to_thread(read_layer_source, self._root, profile_id)

    async def read_manifest(self, profile_id: str) -> ProfileManifest:
        text = await asyncio.to_thread(_read_manifest_text, self._root, profile_id)
        return parse_manifest(text)


def read_layer_source(root: Path, profile_id: str) -> LayerSource:
    profile_dir = root / profile_id
    manifest = parse_manifest(_read_manifest_text(root, profile_id))
    rules = _read_rules(profile_dir)
    term_categories = _read_terms(profile_dir, manifest.gazetteer_dirs)
    allowlist_originals = _read_allowlist(profile_dir)
    if manifest.content_hash is not None:
        verify_integrity(manifest, _content_hash_for_dir(profile_dir))
    return LayerSource(
        profile_id=profile_id,
        manifest=manifest,
        term_categories=term_categories,
        rules=rules,
        allowlist_originals=allowlist_originals,
    )


def build_bundle(
    sources: Sequence[LayerSource], *, cfg: Mapping[str, Any] | None = None
) -> ProfileBundle:
    """Merge ordered ``[core, ..., most-specific]`` layer sources into a bundle."""
    if not sources:
        raise ValueError("build_bundle requires at least one layer source")

    detector_names: list[str] = []
    seen_detectors: set[str] = set()
    for source in sources:
        for name in source.manifest.detectors:
            if name not in seen_detectors:
                seen_detectors.add(name)
                detector_names.append(name)

    rule_items = tuple(rule for source in sources for rule in source.rules.rules)

    # Most-specific layer wins term-label collisions → feed specific-first.
    term_map: dict[str, str] = {}
    for source in reversed(sources):
        for term, label in source.term_categories.items():
            term_map.setdefault(term, label)

    allow_originals: list[str] = []
    seen_allow: set[str] = set()
    for source in sources:
        for original in source.allowlist_originals:
            if original not in seen_allow:
                seen_allow.add(original)
                allow_originals.append(original)

    return ProfileBundle(
        detectors=build_detectors(detector_names, cfg),
        gazetteer=Gazetteer(term_map) if term_map else None,
        rules=Rules(rules=rule_items),
        allowlist=Allowlist(allow_originals),
        policy=PolicyKnobs.merge([source.manifest.policy for source in sources]),
        profile_ids=tuple(source.profile_id for source in sources),
    )


def _read_manifest_text(root: Path, profile_id: str) -> str:
    path = root / profile_id / "profile.toml"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileNotFoundError(f"no profile.toml for {profile_id!r} at {path}") from exc


def _read_rules(profile_dir: Path) -> Rules:
    path = profile_dir / "replace.md"
    if not path.is_file():
        return Rules(rules=())
    return parse(path.read_text(encoding="utf-8"))


def _read_terms(profile_dir: Path, gazetteer_dirs: tuple[str, ...]) -> dict[str, str]:
    dirs = [profile_dir / rel for rel in gazetteer_dirs] if gazetteer_dirs else [profile_dir]
    out: dict[str, str] = {}
    for directory in dirs:
        if directory.is_dir():
            out.update(load_terms(directory))
    return out


def _read_allowlist(profile_dir: Path) -> tuple[str, ...]:
    path = profile_dir / "allowlist.txt"
    if not path.is_file():
        return ()
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return tuple(out)


def _content_hash_for_dir(profile_dir: Path) -> str:
    """Hash every data file under the profile dir except profile.toml itself
    (which carries the declared hash — excluding it avoids self-reference)."""
    parts: list[tuple[str, bytes]] = []
    for path in sorted(profile_dir.rglob("*")):
        if path.is_file() and path.name != "profile.toml":
            parts.append((str(path.relative_to(profile_dir)), path.read_bytes()))
    return compute_content_hash(parts)
