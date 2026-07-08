import pytest

from corp_llm_gateway.profiles import (
    MAX_EXTENDS_DEPTH,
    PolicyKnobs,
    ProfileCycleError,
    ProfileDepthError,
    ProfileManifest,
    ProfileParseError,
    parse_manifest,
    resolve_extends,
)
from corp_llm_gateway.team_config.models import FailPolicyOverrides


def test_parse_minimal() -> None:
    manifest = parse_manifest('name = "core"')
    assert manifest.name == "core"
    assert manifest.extends == ()
    assert manifest.detectors == ()
    assert manifest.policy == PolicyKnobs()


def test_parse_extends_string_normalizes_to_tuple() -> None:
    assert parse_manifest('name="x"\nextends="core"').extends == ("core",)


def test_parse_extends_list() -> None:
    assert parse_manifest('name="x"\nextends=["a","b"]').extends == ("a", "b")


def test_parse_full_policy() -> None:
    text = """
name = "ru"
extends = ["core"]
detectors = ["regex_checksum", "dual_ner"]
gazetteer_dirs = ["."]

[policy]
size_threshold_bytes = 65536
block_payloads = true
dlp_guard = true
oracle_mode = "any_local_finding"
allowed_providers = ["anthropic"]
canary_patterns = ["CANARY-XYZ"]
retention_hot_days = 30

[policy.fail_policy]
pre_pass_down = "fail-closed"
"""
    manifest = parse_manifest(text)
    assert manifest.detectors == ("regex_checksum", "dual_ner")
    assert manifest.gazetteer_dirs == (".",)
    policy = manifest.policy
    assert policy.size_threshold_bytes == 65536
    assert policy.block_payloads is True
    assert policy.dlp_guard is True
    assert policy.oracle_mode == "any_local_finding"
    assert policy.allowed_providers == frozenset({"anthropic"})
    assert policy.canary_patterns == ("CANARY-XYZ",)
    assert policy.retention_hot_days == 30
    assert policy.fail_policy.pre_pass_down == "fail-closed"
    # unset columns keep the M4 defaults
    assert policy.fail_policy.audit_buffer_full == FailPolicyOverrides().audit_buffer_full


def test_parse_missing_name_raises() -> None:
    with pytest.raises(ProfileParseError, match="name"):
        parse_manifest("extends = []")


def test_parse_invalid_toml_raises() -> None:
    with pytest.raises(ProfileParseError):
        parse_manifest("name = ")


def test_parse_bad_fail_policy_value_raises() -> None:
    with pytest.raises(ProfileParseError, match="fail_policy"):
        parse_manifest('name="x"\n[policy.fail_policy]\npre_pass_down = "nope"')


def test_parse_bad_detectors_type_raises() -> None:
    with pytest.raises(ProfileParseError, match="detectors"):
        parse_manifest("name='x'\ndetectors=[1, 2]")


def _reader(manifests: dict[str, ProfileManifest]):
    async def read(profile_id: str) -> ProfileManifest:
        return manifests[profile_id]

    return read


def _m(name: str, *extends: str) -> ProfileManifest:
    return ProfileManifest(name=name, extends=tuple(extends))


async def test_resolve_extends_orders_core_first() -> None:
    manifests = {"core": _m("core"), "mid": _m("mid", "core"), "leaf": _m("leaf", "mid")}
    assert await resolve_extends(["leaf"], _reader(manifests)) == ("core", "mid", "leaf")


async def test_resolve_extends_diamond_dedups_shared_ancestor() -> None:
    manifests = {
        "core": _m("core"),
        "a": _m("a", "core"),
        "b": _m("b", "core"),
        "leaf": _m("leaf", "a", "b"),
    }
    assert await resolve_extends(["leaf"], _reader(manifests)) == ("core", "a", "b", "leaf")


async def test_resolve_extends_cycle_rejected() -> None:
    manifests = {"a": _m("a", "b"), "b": _m("b", "a")}
    with pytest.raises(ProfileCycleError):
        await resolve_extends(["a"], _reader(manifests))


async def test_resolve_extends_self_cycle_rejected() -> None:
    manifests = {"a": _m("a", "a")}
    with pytest.raises(ProfileCycleError):
        await resolve_extends(["a"], _reader(manifests))


async def test_resolve_extends_over_depth_rejected() -> None:
    chain = {f"p{i}": _m(f"p{i}", f"p{i + 1}") for i in range(6)}
    chain["p6"] = _m("p6")
    with pytest.raises(ProfileDepthError):
        await resolve_extends(["p0"], _reader(chain), max_depth=3)


async def test_resolve_extends_default_ceiling_allows_max_chain() -> None:
    chain = {f"p{i}": _m(f"p{i}", f"p{i + 1}") for i in range(MAX_EXTENDS_DEPTH)}
    chain[f"p{MAX_EXTENDS_DEPTH}"] = _m(f"p{MAX_EXTENDS_DEPTH}")
    ordered = await resolve_extends(["p0"], _reader(chain))
    assert ordered[0] == f"p{MAX_EXTENDS_DEPTH}"
    assert ordered[-1] == "p0"
