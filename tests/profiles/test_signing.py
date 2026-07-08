from pathlib import Path

import pytest

from corp_llm_gateway.profiles import (
    FileProfileLoader,
    ProfileIntegrityError,
    ProfileSignatureError,
    parse_manifest,
    verify_signature,
)
from corp_llm_gateway.profiles.file_loader import _content_hash_for_dir

_FLAG = "CORP_PROFILE_REQUIRE_SIGNATURE"


def _write_bundle(root: Path, *, content_hash: str | None = None) -> Path:
    directory = root / "core"
    directory.mkdir(parents=True)
    hash_line = f'content_hash = "{content_hash}"\n' if content_hash is not None else ""
    (directory / "profile.toml").write_text(f'name = "core"\n{hash_line}', encoding="utf-8")
    (directory / "products.txt").write_text("alphaterm\n", encoding="utf-8")
    return directory


def _sealed_bundle(root: Path) -> Path:
    """Write a bundle then seal it with its own correct content_hash."""
    directory = _write_bundle(root)
    good = _content_hash_for_dir(directory)
    (directory / "profile.toml").write_text(
        f'name = "core"\ncontent_hash = "{good}"\n', encoding="utf-8"
    )
    return directory


# hash-integrity — ships now, fail-closed --------------------------------------


async def test_intact_bundle_loads(tmp_path: Path) -> None:
    _sealed_bundle(tmp_path)
    bundle = await FileProfileLoader(tmp_path).load("core")
    assert bundle.profile_ids == ("core",)


async def test_tampered_bundle_refused_at_load(tmp_path: Path) -> None:
    directory = _sealed_bundle(tmp_path)
    (directory / "products.txt").write_text("exfil-me\n", encoding="utf-8")
    with pytest.raises(ProfileIntegrityError, match="content-hash mismatch"):
        await FileProfileLoader(tmp_path).load("core")


async def test_wrong_hash_field_refused_with_clear_error(tmp_path: Path) -> None:
    _write_bundle(tmp_path, content_hash="not-a-real-hash")
    with pytest.raises(ProfileIntegrityError, match="content-hash mismatch"):
        await FileProfileLoader(tmp_path).load("core")


# signature — deferred, gated no-op --------------------------------------------


def test_signature_hook_inert_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert verify_signature(parse_manifest('name = "core"')) is None


async def test_load_not_blocked_by_signature_hook_when_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    _sealed_bundle(tmp_path)
    bundle = await FileProfileLoader(tmp_path).load("core")
    assert bundle.profile_ids == ("core",)


@pytest.mark.parametrize("value", ["1", "true", "on", "YES"])
def test_signature_enforcement_optin_fails_closed(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_FLAG, value)
    with pytest.raises(ProfileSignatureError, match="cosign/PKI"):
        verify_signature(parse_manifest('name = "core"'))


def test_signature_explicit_require_overrides_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    with pytest.raises(ProfileSignatureError):
        verify_signature(parse_manifest('name = "core"'), require=True)
