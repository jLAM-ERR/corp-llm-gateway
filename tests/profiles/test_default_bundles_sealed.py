"""The shipped default bundles are sealed and self-verifying (D6).

D6's fail-closed hash-integrity check only runs when a bundle declares a
``content_hash``. These tests pin that every shipped default under
``profiles/defaults/`` IS sealed (so the mechanism is live for them), that the
declared hash matches the on-disk files (drift guard — a term file edited without
re-running ``python -m corp_llm_gateway.profiles.seal`` fails here), and that
tampering a shipped bundle is caught fail-closed at load.
"""

import shutil
from pathlib import Path

import pytest

import corp_llm_gateway.profiles as profiles_pkg
from corp_llm_gateway.profiles import (
    FileProfileLoader,
    ProfileIntegrityError,
    content_hash_for_dir,
)
from corp_llm_gateway.profiles.seal import seal_profile

BUNDLES_ROOT = Path(profiles_pkg.__file__).parent / "defaults"
_DEFAULTS = ["core", "ru-152fz", "division-x"]


@pytest.mark.parametrize("profile_id", _DEFAULTS)
async def test_default_bundle_is_sealed(profile_id: str) -> None:
    manifest = await FileProfileLoader(BUNDLES_ROOT).read_manifest(profile_id)
    assert manifest.content_hash is not None, f"{profile_id} ships without a content_hash"


@pytest.mark.parametrize("profile_id", _DEFAULTS)
async def test_default_bundle_hash_matches_files(profile_id: str) -> None:
    manifest = await FileProfileLoader(BUNDLES_ROOT).read_manifest(profile_id)
    assert manifest.content_hash == content_hash_for_dir(BUNDLES_ROOT / profile_id)


@pytest.mark.parametrize("profile_id", _DEFAULTS)
async def test_default_bundle_load_verifies_integrity(profile_id: str) -> None:
    # load() runs verify_integrity; a bad seal would raise ProfileIntegrityError.
    bundle = await FileProfileLoader(BUNDLES_ROOT).load(profile_id)
    assert profile_id in bundle.profile_ids


async def test_tampering_a_shipped_bundle_is_caught(tmp_path: Path) -> None:
    shutil.copytree(BUNDLES_ROOT / "core", tmp_path / "core")
    (tmp_path / "core" / "products.txt").write_text("exfil-me\n", encoding="utf-8")
    with pytest.raises(ProfileIntegrityError, match="content-hash mismatch"):
        await FileProfileLoader(tmp_path).load("core")


async def test_seal_profile_is_idempotent_and_correct(tmp_path: Path) -> None:
    shutil.copytree(BUNDLES_ROOT / "ru-152fz", tmp_path / "ru-152fz")
    profile_dir = tmp_path / "ru-152fz"
    first = seal_profile(profile_dir)
    assert first == content_hash_for_dir(profile_dir)
    assert seal_profile(profile_dir) == first  # re-sealing does not change the hash
    manifest = await FileProfileLoader(tmp_path).read_manifest("ru-152fz")
    assert manifest.content_hash == first
