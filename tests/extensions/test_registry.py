from dataclasses import FrozenInstanceError

import pytest

from corp_llm_gateway.extensions import (
    EXTENSION_API_VERSION,
    REGISTRY,
    Extension,
    ExtensionApiVersionError,
    ExtensionKind,
    ExtensionRegistry,
    ExtensionSpec,
)
from corp_llm_gateway.healthz import HealthStatus


class _FakeExtension(Extension):
    def __init__(self, spec: ExtensionSpec, *, healthy: bool = True) -> None:
        self.spec = spec
        self._healthy = healthy

    async def health(self) -> HealthStatus:
        return HealthStatus(self._healthy, self.spec.name)


class _BoomExtension(Extension):
    def __init__(self, spec: ExtensionSpec) -> None:
        self.spec = spec

    async def health(self) -> HealthStatus:
        raise RuntimeError("backend down")


def _spec(
    name: str, kind: ExtensionKind = "audit_sink", *, api_version: str = "1"
) -> ExtensionSpec:
    return ExtensionSpec(
        name=name,
        kind=kind,
        version="1.0.0",
        api_version=api_version,
        capabilities=frozenset({"write"}),
    )


# register / get / enabled ---------------------------------------------------


def test_register_then_get_returns_the_impl() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout")
    reg.register(spec, lambda: _FakeExtension(spec))
    ext = reg.get("audit_sink", "stdout")
    assert isinstance(ext, _FakeExtension)
    assert ext.spec == spec


def test_enabled_returns_impls_of_that_kind_only() -> None:
    reg = ExtensionRegistry()
    stdout = _spec("stdout", "audit_sink")
    langfuse = _spec("langfuse", "audit_sink")
    prom = _spec("prometheus", "metrics")
    reg.register(stdout, lambda: _FakeExtension(stdout))
    reg.register(langfuse, lambda: _FakeExtension(langfuse))
    reg.register(prom, lambda: _FakeExtension(prom))
    assert {e.spec.name for e in reg.enabled("audit_sink")} == {"stdout", "langfuse"}
    assert {e.spec.name for e in reg.enabled("metrics")} == {"prometheus"}


def test_enabled_empty_kind_returns_empty_tuple() -> None:
    reg = ExtensionRegistry()
    assert reg.enabled("provider") == ()


# unknown lookups fail closed, listing the known set (ADR-001 rule 5) ---------


def test_get_unknown_name_raises_listing_known_set() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout")
    reg.register(spec, lambda: _FakeExtension(spec))
    with pytest.raises(ValueError, match="audit_sink:stdout") as exc:
        reg.get("audit_sink", "langfuse")
    assert "langfuse" in str(exc.value)


def test_get_unknown_kind_raises() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout", "audit_sink")
    reg.register(spec, lambda: _FakeExtension(spec))
    with pytest.raises(ValueError):
        reg.get("metrics", "stdout")


# validate_api_version fails closed -----------------------------------------


def test_validate_api_version_passes_on_match() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout", api_version=EXTENSION_API_VERSION)
    reg.register(spec, lambda: _FakeExtension(spec))
    reg.validate_api_version(EXTENSION_API_VERSION)


def test_validate_api_version_fails_closed_on_mismatch() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout", api_version="99")
    reg.register(spec, lambda: _FakeExtension(spec))
    with pytest.raises(ExtensionApiVersionError):
        reg.validate_api_version(EXTENSION_API_VERSION)


def test_validate_api_version_empty_registry_passes() -> None:
    ExtensionRegistry().validate_api_version(EXTENSION_API_VERSION)


# health_all aggregation ----------------------------------------------------


async def test_health_all_aggregates_each_extension() -> None:
    reg = ExtensionRegistry()
    up = _spec("stdout", "audit_sink")
    down = _spec("prometheus", "metrics")
    reg.register(up, lambda: _FakeExtension(up, healthy=True))
    reg.register(down, lambda: _FakeExtension(down, healthy=False))
    report = await reg.health_all()
    assert report["audit_sink:stdout"].healthy is True
    assert report["metrics:prometheus"].healthy is False


async def test_health_all_catches_a_raising_health() -> None:
    reg = ExtensionRegistry()
    spec = _spec("jaeger", "tracing")
    reg.register(spec, lambda: _BoomExtension(spec))
    report = await reg.health_all()
    assert report["tracing:jaeger"].healthy is False
    assert "RuntimeError" in report["tracing:jaeger"].detail


async def test_health_all_empty_registry_is_empty() -> None:
    assert await ExtensionRegistry().health_all() == {}


# spec + module singleton ---------------------------------------------------


def test_spec_defaults_fail_closed_and_accepts_m4_vocab() -> None:
    default = ExtensionSpec(name="x", kind="metrics", version="1", api_version="1")
    assert default.fail_policy == "fail-closed"
    assert default.capabilities == frozenset()
    relaxed = ExtensionSpec(
        name="x", kind="metrics", version="1", api_version="1", fail_policy="continue"
    )
    assert relaxed.fail_policy == "continue"


def test_spec_is_frozen() -> None:
    spec = _spec("stdout")
    with pytest.raises(FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]


def test_module_exposes_singleton_and_api_version() -> None:
    assert isinstance(REGISTRY, ExtensionRegistry)
    assert EXTENSION_API_VERSION == "1"


def test_discover_is_a_noop_seam() -> None:
    reg = ExtensionRegistry()
    assert reg.discover() is None
    assert reg.enabled("audit_sink") == ()
