"""Resolve a team's selected profile set to one merged ProfileBundle.

``resolve_team`` is the team-facing entry: it reads the ``profile_ids`` a team
selects (via its ``TeamConfig``, keyed by the ``AuthContext.team_id`` routing key
the orchestrator already receives) and resolves them. ``resolve`` expands
``extends`` (via ``manifest.resolve_extends``) into an ordered ``[core, ...,
most-specific]`` layer list, loads each layer's source, and merges them with
``build_bundle``. Results are memoized by the ordered layer-key, so two teams that
resolve to the same layers share one bundle. Empty ``profile_ids`` → the empty
core/default bundle (today's behavior: composition adds nothing).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from corp_llm_gateway.profiles.base import PolicyKnobs, ProfileBundle
from corp_llm_gateway.profiles.file_loader import FileProfileLoader, build_bundle
from corp_llm_gateway.profiles.manifest import MAX_EXTENDS_DEPTH, resolve_extends
from corp_llm_gateway.rules.models import Rules
from corp_llm_gateway.sanitizer.allowlist import Allowlist

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from corp_llm_gateway.team_config.models import TeamConfig


class ProfileResolver:
    def __init__(
        self,
        loader: FileProfileLoader,
        *,
        cfg: Mapping[str, Any] | None = None,
        max_depth: int = MAX_EXTENDS_DEPTH,
        default_profile_ids: Sequence[str] = (),
    ) -> None:
        self._loader = loader
        self._cfg: Mapping[str, Any] = dict(cfg) if cfg is not None else {}
        self._max_depth = max_depth
        # Layers to resolve when a team selects nothing; empty → the empty bundle.
        self._default_profile_ids = tuple(default_profile_ids)
        self._cache: dict[tuple[str, ...], ProfileBundle] = {}
        self._lock = asyncio.Lock()

    async def resolve_team(self, config: TeamConfig) -> ProfileBundle:
        """Resolve the profile set a team selects into one merged bundle.

        Keys off ``config.profile_ids`` (fetched by the ``team_id`` routing key) —
        no separate profile-routing key. Empty ``profile_ids`` → the empty
        core/default bundle.
        """
        effective = config.profile_ids or self._default_profile_ids
        if not effective:
            return await self._resolve_ordered(())
        return await self.resolve(effective)

    async def resolve(self, profile_ids: Sequence[str]) -> ProfileBundle:
        if not profile_ids:
            raise ValueError("resolve requires at least one profile_id")
        ordered = await resolve_extends(
            profile_ids, self._loader.read_manifest, max_depth=self._max_depth
        )
        return await self._resolve_ordered(ordered)

    async def _resolve_ordered(self, ordered: tuple[str, ...]) -> ProfileBundle:
        cached = self._cache.get(ordered)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cache.get(ordered)
            if cached is not None:
                return cached
            if ordered:
                sources = [await self._loader.read_source(profile_id) for profile_id in ordered]
                bundle = build_bundle(sources, cfg=self._cfg)
            else:
                bundle = _empty_bundle()
            self._cache[ordered] = bundle
            return bundle


def _empty_bundle() -> ProfileBundle:
    """The no-profiles bundle: contributes nothing (today's default behavior)."""
    return ProfileBundle(
        detectors=(),
        gazetteer=None,
        rules=Rules(rules=()),
        allowlist=Allowlist(()),
        policy=PolicyKnobs(),
        profile_ids=(),
    )
