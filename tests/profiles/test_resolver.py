"""Team-facing profile resolution: TeamConfig.profile_ids → merged ProfileBundle.

Complements the ProfileResolver cases in test_loader.py (which drive resolve()
by raw profile_ids). Here the entry point is resolve_team(config), keyed off the
team_id routing key via its TeamConfig.
"""

from pathlib import Path

import pytest

from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.profiles import (
    FileProfileLoader,
    PolicyKnobs,
    ProfileBundle,
    ProfileNotFoundError,
    ProfileResolver,
    bundle_fingerprint,
)
from corp_llm_gateway.rules.gazetteer import Gazetteer
from corp_llm_gateway.rules.models import Rule, Rules
from corp_llm_gateway.sanitizer.allowlist import Allowlist
from corp_llm_gateway.team_config import TeamConfig


def _write_profile(root: Path, profile_id: str, toml: str, *, products: str | None = None) -> None:
    directory = root / profile_id
    directory.mkdir(parents=True)
    (directory / "profile.toml").write_text(toml, encoding="utf-8")
    if products is not None:
        (directory / "products.txt").write_text(products, encoding="utf-8")


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
        "ru",
        toml='name = "ru"\nextends = ["core"]\n'
        "[policy]\nsize_threshold_bytes = 50000\nblock_payloads = true\n"
        'allowed_providers = ["anthropic"]\n',
        products="ruterm\n",
    )
    _write_profile(
        tmp_path,
        "division",
        toml='name = "division"\nextends = ["ru"]\n[policy]\nsize_threshold_bytes = 30000\n',
    )
    return tmp_path


def _team(team_id: str, profile_ids: tuple[str, ...]) -> TeamConfig:
    return TeamConfig(team_id=team_id, name=f"Team {team_id}", profile_ids=profile_ids)


# resolve_team: ordered layers + merge -------------------------------------


async def test_resolve_team_expands_to_ordered_layers(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    bundle = await resolver.resolve_team(_team("t1", ("division",)))
    assert bundle.profile_ids == ("core", "ru", "division")


async def test_resolve_team_merges_bundle(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    bundle = await resolver.resolve_team(_team("t1", ("division",)))
    assert {type(d) for d in bundle.detectors} == {RegexChecksumDetector}
    # most-restrictive-wins: deepest layer's threshold, OR'd block flag, provider set
    assert bundle.policy.size_threshold_bytes == 30000
    assert bundle.policy.block_payloads is True
    assert bundle.policy.allowed_providers == frozenset({"anthropic"})
    assert bundle.gazetteer is not None
    assert await bundle.gazetteer.detect("coreterm here")
    assert await bundle.gazetteer.detect("ruterm here")


# empty profile_ids → empty/default bundle ----------------------------------


async def test_resolve_team_empty_yields_empty_bundle(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(tmp_path))
    bundle = await resolver.resolve_team(_team("t1", ()))
    assert bundle.profile_ids == ()
    assert bundle.detectors == ()
    assert bundle.gazetteer is None
    assert bundle.rules.rules == ()


async def test_resolve_team_empty_bundle_memoized(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(tmp_path))
    first = await resolver.resolve_team(_team("t1", ()))
    second = await resolver.resolve_team(_team("t2", ()))
    assert first is second


async def test_resolve_team_empty_falls_back_to_default_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path, "core", toml='name = "core"\ndetectors = ["regex_checksum"]\n')
    resolver = ProfileResolver(FileProfileLoader(tmp_path), default_profile_ids=("core",))
    bundle = await resolver.resolve_team(_team("t1", ()))
    assert bundle.profile_ids == ("core",)
    assert {type(d) for d in bundle.detectors} == {RegexChecksumDetector}


# unknown profile fails closed ----------------------------------------------


async def test_resolve_team_unknown_profile_raises(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    with pytest.raises(ProfileNotFoundError, match="ghost"):
        await resolver.resolve_team(_team("t1", ("ghost",)))


async def test_resolve_team_unknown_parent_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path, "leaf", toml='name = "leaf"\nextends = ["missing"]\n')
    resolver = ProfileResolver(FileProfileLoader(tmp_path))
    with pytest.raises(ProfileNotFoundError, match="missing"):
        await resolver.resolve_team(_team("t1", ("leaf",)))


async def test_resolve_team_unknown_does_not_yield_default(tmp_path: Path) -> None:
    # A default is configured, but an unknown selection must still raise —
    # fail closed, never silently degrade to the default/core bundle.
    _write_profile(tmp_path, "core", toml='name = "core"\n')
    resolver = ProfileResolver(FileProfileLoader(tmp_path), default_profile_ids=("core",))
    with pytest.raises(ProfileNotFoundError):
        await resolver.resolve_team(_team("t1", ("ghost",)))


# memoization keyed by resolved layer-key -----------------------------------


async def test_resolve_team_same_layer_key_shared_across_teams(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    a = await resolver.resolve_team(_team("t1", ("division",)))
    b = await resolver.resolve_team(_team("t2", ("division",)))
    assert a is b


async def test_resolve_team_reresolves_when_layer_key_changes(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    division = await resolver.resolve_team(_team("t1", ("division",)))
    ru = await resolver.resolve_team(_team("t1", ("ru",)))
    assert division is not ru
    assert division.profile_ids == ("core", "ru", "division")
    assert ru.profile_ids == ("core", "ru")


# bundle_fingerprint: D3 cache-key identity ---------------------------------


def _bundle(
    *,
    profile_ids: tuple[str, ...] = ("core",),
    detectors: tuple[object, ...] = (),
    gazetteer: Gazetteer | None = None,
    rules: Rules | None = None,
    policy: PolicyKnobs | None = None,
) -> ProfileBundle:
    return ProfileBundle(
        detectors=detectors,  # type: ignore[arg-type]
        gazetteer=gazetteer,
        rules=rules or Rules(rules=()),
        allowlist=Allowlist(()),
        policy=policy or PolicyKnobs(),
        profile_ids=profile_ids,
    )


def test_bundle_fingerprint_is_stable_and_hex() -> None:
    fp = bundle_fingerprint(_bundle())
    assert fp == bundle_fingerprint(_bundle())
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_bundle_fingerprint_differs_by_layer_key() -> None:
    assert bundle_fingerprint(_bundle(profile_ids=("core",))) != bundle_fingerprint(
        _bundle(profile_ids=("core", "ru"))
    )


def test_bundle_fingerprint_differs_by_detector_set() -> None:
    assert bundle_fingerprint(_bundle(detectors=())) != bundle_fingerprint(
        _bundle(detectors=(RegexChecksumDetector(),))
    )


def test_bundle_fingerprint_differs_by_gazetteer_presence() -> None:
    assert bundle_fingerprint(_bundle(gazetteer=None)) != bundle_fingerprint(
        _bundle(gazetteer=Gazetteer({"sistema": "PRODUCT"}))
    )


def test_bundle_fingerprint_differs_by_policy_knob() -> None:
    assert bundle_fingerprint(_bundle(policy=PolicyKnobs())) != bundle_fingerprint(
        _bundle(policy=PolicyKnobs(block_payloads=True))
    )
    # allowed_providers = None (no restriction) must differ from the empty set.
    assert bundle_fingerprint(
        _bundle(policy=PolicyKnobs(allowed_providers=None))
    ) != bundle_fingerprint(_bundle(policy=PolicyKnobs(allowed_providers=frozenset())))


def test_bundle_fingerprint_differs_by_rules() -> None:
    assert bundle_fingerprint(_bundle(rules=Rules(rules=()))) != bundle_fingerprint(
        _bundle(rules=Rules(rules=(Rule("acme", "[CO]"),)))
    )


async def test_resolved_bundles_get_distinct_fingerprints(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    division = await resolver.resolve_team(_team("t1", ("division",)))
    ru = await resolver.resolve_team(_team("t1", ("ru",)))
    assert bundle_fingerprint(division) != bundle_fingerprint(ru)


async def test_shared_resolved_bundle_shares_fingerprint(tmp_path: Path) -> None:
    resolver = ProfileResolver(FileProfileLoader(_layered_root(tmp_path)))
    a = await resolver.resolve_team(_team("t1", ("division",)))
    b = await resolver.resolve_team(_team("t2", ("division",)))
    assert a is b
    assert bundle_fingerprint(a) == bundle_fingerprint(b)
