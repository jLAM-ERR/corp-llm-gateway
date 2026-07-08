"""Boundary / error-path hardening for the C1 extension registry.

Companion to ``test_registry.py`` (happy-path). Focus: duplicate registration,
the api-version fail-closed seam, aggregation that must not crash or fail-open,
factory/error lifecycle, and the import-side-effect-free singleton.
"""

import importlib
import sys

import pytest

import corp_llm_gateway.config as config_mod
from corp_llm_gateway.extensions import (
    EXTENSION_API_VERSION,
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


class _BoomHealth(Extension):
    def __init__(self, spec: ExtensionSpec) -> None:
        self.spec = spec

    async def health(self) -> HealthStatus:
        raise RuntimeError("backend down")


def _spec(
    name: str, kind: ExtensionKind = "audit_sink", *, api_version: str = "1", version: str = "1.0.0"
) -> ExtensionSpec:
    return ExtensionSpec(name=name, kind=kind, version=version, api_version=api_version)


# duplicate registration -----------------------------------------------------


def test_duplicate_registration_last_write_wins() -> None:
    # register() has no documented duplicate policy; it silently overwrites the
    # same (kind, name) pair. Pin the observable last-write-wins so a future
    # edit cannot flip it unnoticed (reviewer: confirm reject-vs-overwrite).
    reg = ExtensionRegistry()
    first = _spec("stdout", version="1")
    second = _spec("stdout", version="2")
    reg.register(first, lambda: _FakeExtension(first))
    reg.register(second, lambda: _FakeExtension(second))
    survivors = reg.enabled("audit_sink")
    assert len(survivors) == 1
    assert reg.get("audit_sink", "stdout").spec.version == "2"


# api-version gate lives in validate_api_version(), not register()/get() ------


def test_register_accepts_mismatched_api_version() -> None:
    # register() is not the gate: a version-incompatible spec registers, and
    # get() will hand it back. The fail-closed check is validate_api_version(),
    # expected to run at bootstrap (see reviewer note on the opt-in gate).
    reg = ExtensionRegistry()
    spec = _spec("stdout", api_version="99")
    reg.register(spec, lambda: _FakeExtension(spec))
    assert reg.get("audit_sink", "stdout").spec.api_version == "99"


def test_validate_api_version_one_bad_among_good_fails_closed() -> None:
    reg = ExtensionRegistry()
    good = _spec("stdout", "audit_sink", api_version=EXTENSION_API_VERSION)
    bad = _spec("prometheus", "metrics", api_version="7")
    reg.register(good, lambda: _FakeExtension(good))
    reg.register(bad, lambda: _FakeExtension(bad))
    with pytest.raises(ExtensionApiVersionError) as exc:
        reg.validate_api_version(EXTENSION_API_VERSION)
    msg = str(exc.value)
    assert "metrics:prometheus" in msg
    assert "'7'" in msg


# unknown lookups fail closed, listing the (possibly empty) known set ---------


def test_get_on_empty_registry_raises_listing_empty_set() -> None:
    reg = ExtensionRegistry()
    with pytest.raises(ValueError) as exc:
        reg.get("provider", "corp-vllm")
    assert str(exc.value).endswith("expected one of ()")


def test_get_propagates_factory_construction_error() -> None:
    # Unlike health_all(), get() is not defensive: a raising factory surfaces
    # the real error to the caller instead of being masked.
    reg = ExtensionRegistry()
    spec = _spec("stdout")

    def _explode() -> Extension:
        raise RuntimeError("cannot build sink")

    reg.register(spec, _explode)
    with pytest.raises(RuntimeError, match="cannot build sink"):
        reg.get("audit_sink", "stdout")


def test_get_and_enabled_build_fresh_instances_each_call() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout")
    reg.register(spec, lambda: _FakeExtension(spec))
    assert reg.get("audit_sink", "stdout") is not reg.get("audit_sink", "stdout")
    (first,) = reg.enabled("audit_sink")
    (second,) = reg.enabled("audit_sink")
    assert first is not second


# health_all aggregates, never crashes, never fails open ---------------------


async def test_health_all_mixes_healthy_unhealthy_and_raising_without_fail_open() -> None:
    reg = ExtensionRegistry()
    up = _spec("stdout", "audit_sink")
    down = _spec("prometheus", "metrics")
    boom = _spec("jaeger", "tracing")
    reg.register(up, lambda: _FakeExtension(up, healthy=True))
    reg.register(down, lambda: _FakeExtension(down, healthy=False))
    reg.register(boom, lambda: _BoomHealth(boom))
    report = await reg.health_all()
    assert set(report) == {"audit_sink:stdout", "metrics:prometheus", "tracing:jaeger"}
    assert report["audit_sink:stdout"].healthy is True
    assert report["metrics:prometheus"].healthy is False
    # an errored health is UNHEALTHY, never silently healthy
    assert report["tracing:jaeger"].healthy is False
    assert "RuntimeError" in report["tracing:jaeger"].detail


async def test_health_all_catches_factory_construction_error() -> None:
    reg = ExtensionRegistry()
    spec = _spec("stdout")

    def _explode() -> Extension:
        raise RuntimeError("cannot build sink")

    reg.register(spec, _explode)
    report = await reg.health_all()
    assert report["audit_sink:stdout"].healthy is False
    assert "RuntimeError" in report["audit_sink:stdout"].detail


# unicode / multi-byte names round-trip through keys -------------------------


async def test_non_ascii_extension_name_round_trips() -> None:
    reg = ExtensionRegistry()
    spec = _spec("аудит-логер")
    reg.register(spec, lambda: _FakeExtension(spec))
    assert reg.get("audit_sink", "аудит-логер").spec.name == "аудит-логер"
    report = await reg.health_all()
    assert report["audit_sink:аудит-логер"].healthy is True


# module-level REGISTRY is import-side-effect-free ---------------------------


def test_importing_extensions_package_reads_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-execute the extensions package bodies with config wired to explode.
    # A clean import proves no config read (no import-time I/O) at module load.
    def _boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("config was read at extensions import time")

    monkeypatch.setattr(config_mod, "get", _boom)
    monkeypatch.setattr(config_mod, "get_required", _boom)
    for name in list(sys.modules):
        if name == "corp_llm_gateway.extensions" or name.startswith("corp_llm_gateway.extensions."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module("corp_llm_gateway.extensions")
    assert isinstance(module.REGISTRY, module.ExtensionRegistry)
