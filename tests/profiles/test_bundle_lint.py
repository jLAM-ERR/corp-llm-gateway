"""Bundle-lint coverage (D8).

Positive: each baked-in default bundle (core / ru-152fz / division-x) parses,
lists only registered detectors, has parseable term files + replace.md, and its
extends chain resolves with no cycle / within max depth. Negative: hand-built
fixture bundles (malformed manifest, unknown detector, cyclic + too-deep extends,
malformed replace.md, missing profile.toml) are all rejected by the lint.
"""

from pathlib import Path

import pytest

import corp_llm_gateway.profiles as profiles_pkg
from corp_llm_gateway.profiles import (
    DETECTOR_REGISTRY,
    BundleLintError,
    FileProfileLoader,
    discover_profiles,
    lint_bundle,
    lint_root,
)

BUNDLES_ROOT = Path(profiles_pkg.__file__).parent / "defaults"

DEFAULT_BUNDLES = ["core", "ru-152fz", "division-x"]


# positive: the shipped default bundles lint clean -----------------------------


@pytest.mark.parametrize("profile_id", DEFAULT_BUNDLES)
async def test_default_bundle_lints_clean(profile_id: str) -> None:
    manifest = await lint_bundle(BUNDLES_ROOT, profile_id)
    assert manifest.name == profile_id


@pytest.mark.parametrize("profile_id", DEFAULT_BUNDLES)
async def test_default_bundle_detectors_are_all_registered(profile_id: str) -> None:
    manifest = await FileProfileLoader(BUNDLES_ROOT).read_manifest(profile_id)
    for name in manifest.detectors:
        assert name in DETECTOR_REGISTRY, f"{profile_id}: detector {name!r} not in registry"


async def test_lint_root_covers_every_default_bundle() -> None:
    ids = await lint_root(BUNDLES_ROOT)
    assert set(ids) == set(DEFAULT_BUNDLES)


def test_discover_profiles_finds_bundle_dirs() -> None:
    assert set(discover_profiles(BUNDLES_ROOT)) == set(DEFAULT_BUNDLES)


# fixture helpers --------------------------------------------------------------


def _write_profile(
    root: Path, profile_id: str, toml: str, *, replace_md: str | None = None
) -> None:
    profile_dir = root / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.toml").write_text(toml, encoding="utf-8")
    if replace_md is not None:
        (profile_dir / "replace.md").write_text(replace_md, encoding="utf-8")


# negative: malformed / unknown-detector / cyclic bundles are rejected ---------


async def test_lint_rejects_malformed_manifest(tmp_path: Path) -> None:
    _write_profile(tmp_path, "bad", "name = ")  # invalid TOML
    with pytest.raises(BundleLintError, match="bad"):
        await lint_bundle(tmp_path, "bad")


async def test_lint_rejects_unknown_detector(tmp_path: Path) -> None:
    _write_profile(tmp_path, "bad", 'name = "bad"\ndetectors = ["nope"]')
    with pytest.raises(BundleLintError, match="unknown detector") as exc:
        await lint_bundle(tmp_path, "bad")
    for known in DETECTOR_REGISTRY:
        assert known in str(exc.value)


async def test_lint_rejects_cyclic_extends(tmp_path: Path) -> None:
    _write_profile(tmp_path, "a", 'name = "a"\nextends = ["b"]')
    _write_profile(tmp_path, "b", 'name = "b"\nextends = ["a"]')
    with pytest.raises(BundleLintError, match="extends"):
        await lint_bundle(tmp_path, "a")


async def test_lint_rejects_over_depth_extends(tmp_path: Path) -> None:
    for i in range(4):
        _write_profile(tmp_path, f"p{i}", f'name = "p{i}"\nextends = ["p{i + 1}"]')
    _write_profile(tmp_path, "p4", 'name = "p4"')
    with pytest.raises(BundleLintError, match="extends"):
        await lint_bundle(tmp_path, "p0", max_depth=2)


async def test_lint_rejects_malformed_replace_md(tmp_path: Path) -> None:
    _write_profile(tmp_path, "bad", 'name = "bad"', replace_md="- not a valid rule line")
    with pytest.raises(BundleLintError, match="bad"):
        await lint_bundle(tmp_path, "bad")


async def test_lint_rejects_missing_profile_toml(tmp_path: Path) -> None:
    (tmp_path / "ghost").mkdir()
    with pytest.raises(BundleLintError):
        await lint_bundle(tmp_path, "ghost")


async def test_lint_rejects_extends_to_missing_parent(tmp_path: Path) -> None:
    _write_profile(tmp_path, "leaf", 'name = "leaf"\nextends = ["nonexistent"]')
    with pytest.raises(BundleLintError, match="extends"):
        await lint_bundle(tmp_path, "leaf")


async def test_lint_root_propagates_a_bad_bundle(tmp_path: Path) -> None:
    _write_profile(tmp_path, "ok", 'name = "ok"')
    _write_profile(tmp_path, "bad", 'name = "bad"\ndetectors = ["nope"]')
    with pytest.raises(BundleLintError, match="unknown detector"):
        await lint_root(tmp_path)


def test_discover_profiles_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(BundleLintError, match="does not exist"):
        discover_profiles(tmp_path / "nope")
