"""Resolve a set of profile ids to one merged ProfileBundle.

``resolve`` expands ``extends`` (via ``manifest.resolve_extends``) into an ordered
``[core, ..., most-specific]`` layer list, loads each layer's source, and merges
them with ``build_bundle``. Results are memoized by the ordered layer-key, so two
teams that resolve to the same layers share one bundle.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from corp_llm_gateway.profiles.file_loader import FileProfileLoader, build_bundle
from corp_llm_gateway.profiles.manifest import MAX_EXTENDS_DEPTH, resolve_extends

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from corp_llm_gateway.profiles.base import ProfileBundle


class ProfileResolver:
    def __init__(
        self,
        loader: FileProfileLoader,
        *,
        cfg: Mapping[str, Any] | None = None,
        max_depth: int = MAX_EXTENDS_DEPTH,
    ) -> None:
        self._loader = loader
        self._cfg: Mapping[str, Any] = dict(cfg) if cfg is not None else {}
        self._max_depth = max_depth
        self._cache: dict[tuple[str, ...], ProfileBundle] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, profile_ids: Sequence[str]) -> ProfileBundle:
        if not profile_ids:
            raise ValueError("resolve requires at least one profile_id")
        ordered = await resolve_extends(
            profile_ids, self._loader.read_manifest, max_depth=self._max_depth
        )
        cached = self._cache.get(ordered)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cache.get(ordered)
            if cached is not None:
                return cached
            sources = [await self._loader.read_source(profile_id) for profile_id in ordered]
            bundle = build_bundle(sources, cfg=self._cfg)
            self._cache[ordered] = bundle
            return bundle
