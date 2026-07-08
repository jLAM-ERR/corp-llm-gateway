import asyncio
from pathlib import Path

import pytest

from corp_llm_gateway.detectors.dual_ner import DualNerDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.profiles import (
    CachedProfileLoader,
    FileProfileLoader,
    PolicyKnobs,
    ProfileBundle,
    ProfileCycleError,
    ProfileIntegrityError,
    ProfileLoader,
    ProfileNotFoundError,
    ProfileResolver,
)
from corp_llm_gateway.profiles.file_loader import _content_hash_for_dir
from corp_llm_gateway.rules.models import Rules
from corp_llm_gateway.sanitizer.allowlist import Allowlist


def _write_profile(
    root: Path,
    profile_id: str,
    toml: str,
    *,
    products: str | None = None,
    replace_md: str | None = None,
    allowlist: str | None = None,
) -> Path:
    directory = root / profile_id
    directory.mkdir(parents=True)
    (directory / "profile.toml").write_text(toml, encoding="utf-8")
    if products is not None:
        (directory / "products.txt").write_text(products, encoding="utf-8")
    if replace_md is not None:
        (directory / "replace.md").write_text(replace_md, encoding="utf-8")
    if allowlist is not None:
        (directory / "allowlist.txt").write_text(allowlist, encoding="utf-8")
    return directory


# FileProfileLoader.load ------------------------------------------------------


async def test_load_single_profile_bundle(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "core",
        toml='name = "core"\ndetectors = ["regex_checksum"]\n',
        products="alphaterm\n",
        replace_md="- alice → [N1]\n",
        allowlist="synthetic@example.com\n",
    )
    bundle = await FileProfileLoader(tmp_path).load("core")
    assert isinstance(bundle, ProfileBundle)
    assert isinstance(bundle.detectors[0], RegexChecksumDetector)
    assert bundle.rules.rules[0].pattern == "alice"
    assert bundle.profile_ids == ("core",)
    assert bundle.gazetteer is not None
    findings = await bundle.gazetteer.detect("we ship alphaterm today")
    assert any(f.label == "PRODUCT" for f in findings)


async def test_load_no_optional_files(tmp_path: Path) -> None:
    _write_profile(tmp_path, "bare", toml='name = "bare"\n')
    bundle = await FileProfileLoader(tmp_path).load("bare")
    assert bundle.detectors == ()
    assert bundle.gazetteer is None
    assert bundle.rules.rules == ()
    assert bundle.profile_ids == ("bare",)


async def test_load_missing_profile_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileNotFoundError, match="ghost"):
        await FileProfileLoader(tmp_path).load("ghost")


async def test_load_allowlist_applied_and_comments_skipped(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "core",
        toml='name = "core"\n',
        allowlist="synthetic@example.com\n# a comment\n",
    )
    bundle = await FileProfileLoader(tmp_path).load("core")
    # allowlisted non-secret drops out of the pair set
    assert bundle.allowlist.filter_pairs((("synthetic@example.com", "[EMAIL_1]"),)) == ()
    # a non-allowlisted value survives
    kept = bundle.allowlist.filter_pairs((("real@corp.lan", "[EMAIL_1]"),))
    assert kept == (("real@corp.lan", "[EMAIL_1]"),)


async def test_read_manifest(tmp_path: Path) -> None:
    _write_profile(tmp_path, "core", toml='name = "core"\nextends = ["base"]\n')
    manifest = await FileProfileLoader(tmp_path).read_manifest("core")
    assert manifest.name == "core"
    assert manifest.extends == ("base",)


# content-hash integrity (D6 seam) -------------------------------------------


async def test_content_hash_wrong_rejected(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "core",
        toml='name = "core"\ncontent_hash = "deadbeef"\n',
        products="alphaterm\n",
    )
    with pytest.raises(ProfileIntegrityError):
        await FileProfileLoader(tmp_path).load("core")


async def test_content_hash_correct_loads_then_tamper_rejected(tmp_path: Path) -> None:
    directory = _write_profile(tmp_path, "core", toml='name = "core"\n', products="alphaterm\n")
    good = _content_hash_for_dir(directory)
    (directory / "profile.toml").write_text(
        f'name = "core"\ncontent_hash = "{good}"\n', encoding="utf-8"
    )
    bundle = await FileProfileLoader(tmp_path).load("core")
    assert bundle.profile_ids == ("core",)

    (directory / "products.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ProfileIntegrityError):
        await FileProfileLoader(tmp_path).load("core")


# CachedProfileLoader ---------------------------------------------------------


def _empty_bundle() -> ProfileBundle:
    return ProfileBundle(
        detectors=(),
        gazetteer=None,
        rules=Rules(rules=()),
        allowlist=Allowlist(()),
        policy=PolicyKnobs(),
        profile_ids=("x",),
    )


class _CountingLoader(ProfileLoader):
    def __init__(self, bundle: ProfileBundle, delay: float = 0.0) -> None:
        self._bundle = bundle
        self._delay = delay
        self.calls = 0

    async def load(self, profile_id: str) -> ProfileBundle:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._bundle


async def test_cached_loader_hits_within_ttl() -> None:
    inner = _CountingLoader(_empty_bundle())
    cached = CachedProfileLoader(inner, ttl_seconds=60)
    await cached.load("x")
    await cached.load("x")
    assert inner.calls == 1


async def test_cached_loader_refreshes_after_ttl() -> None:
    inner = _CountingLoader(_empty_bundle())
    cached = CachedProfileLoader(inner, ttl_seconds=1)
    await cached.load("x")
    await asyncio.sleep(1.2)
    await cached.load("x")
    assert inner.calls == 2


async def test_cached_loader_concurrent_loads_dedupe() -> None:
    inner = _CountingLoader(_empty_bundle(), delay=0.2)
    cached = CachedProfileLoader(inner, ttl_seconds=60)
    await asyncio.gather(*(cached.load("x") for _ in range(8)))
    assert inner.calls == 1


# ProfileResolver -------------------------------------------------------------


def _layered_root(tmp_path: Path) -> Path:
    _write_profile(
        tmp_path,
        "core",
        toml='name = "core"\ndetectors = ["regex_checksum"]\n'
        "[policy]\nsize_threshold_bytes = 100000\n",
        products="coreterm\n",
    )
    _write_profile(
        tmp_path,
        "mid",
        toml='name = "mid"\nextends = ["core"]\ndetectors = ["dual_ner"]\n'
        "[policy]\nsize_threshold_bytes = 80000\n",
    )
    _write_profile(
        tmp_path,
        "leaf",
        toml='name = "leaf"\nextends = ["mid"]\n'
        "[policy]\nsize_threshold_bytes = 50000\nblock_payloads = true\n"
        'allowed_providers = ["anthropic"]\n',
        products="leafterm\n",
    )
    return tmp_path


async def test_resolver_orders_and_merges_layers(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    bundle = await resolver.resolve(["leaf"])
    assert bundle.profile_ids == ("core", "mid", "leaf")
    assert {type(d).__name__ for d in bundle.detectors} == {
        "RegexChecksumDetector",
        "DualNerDetector",
    }
    assert bundle.policy.size_threshold_bytes == 50000
    assert bundle.policy.block_payloads is True
    assert bundle.policy.allowed_providers == frozenset({"anthropic"})
    assert bundle.gazetteer is not None
    assert await bundle.gazetteer.detect("coreterm here")
    assert await bundle.gazetteer.detect("leafterm here")


async def test_resolver_detector_and_isinstance(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    bundle = await resolver.resolve(["leaf"])
    kinds = {type(d) for d in bundle.detectors}
    assert RegexChecksumDetector in kinds
    assert DualNerDetector in kinds


async def test_resolver_memoizes_by_layer_key(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    first = await resolver.resolve(["leaf"])
    second = await resolver.resolve(["leaf"])
    assert first is second


async def test_resolver_single_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path, "core", toml='name = "core"\n')
    bundle = await ProfileResolver(FileProfileLoader(tmp_path)).resolve(["core"])
    assert bundle.profile_ids == ("core",)


async def test_resolver_empty_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        await ProfileResolver(FileProfileLoader(tmp_path)).resolve([])


async def test_resolver_cycle_via_files_rejected(tmp_path: Path) -> None:
    _write_profile(tmp_path, "a", toml='name = "a"\nextends = ["b"]\n')
    _write_profile(tmp_path, "b", toml='name = "b"\nextends = ["a"]\n')
    with pytest.raises(ProfileCycleError):
        await ProfileResolver(FileProfileLoader(tmp_path)).resolve(["a"])
