"""Corp NER as the first detector-kind extension (B4)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from corp_llm_gateway.extensions import (
    EXTENSION_API_VERSION,
    Extension,
    ExtensionRegistry,
)
from corp_llm_gateway.extensions.corp_ner import (
    READY_PATH,
    CorpNerExtension,
    register_corp_ner,
)
from corp_llm_gateway.healthz import ExtensionsCheck

_BASE_URL = "https://corp-ner.test"

Handler = Callable[[httpx.Request], httpx.Response]


def _extension(handler: Handler) -> CorpNerExtension:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpNerExtension(_BASE_URL, http=http)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ready"})


# ---- spec -------------------------------------------------------------------


def test_spec_is_a_fail_closed_detector_extension() -> None:
    spec = _extension(_ok).spec

    assert spec.name == "corp_ner"
    assert spec.kind == "detector"
    assert spec.api_version == EXTENSION_API_VERSION
    # Fail-closed is unconditional: corp NER sits on the egress path (invariant 6).
    assert spec.fail_policy == "fail-closed"


def test_registered_extension_passes_the_api_version_gate() -> None:
    reg = ExtensionRegistry()
    register_corp_ner(reg, _extension(_ok))

    reg.validate_api_version(EXTENSION_API_VERSION)  # must not raise


def test_registry_returns_the_same_live_instance() -> None:
    reg = ExtensionRegistry()
    ext = register_corp_ner(reg, _extension(_ok))

    assert reg.get("detector", "corp_ner") is ext
    assert reg.get("detector", "corp_ner") is reg.get("detector", "corp_ner")


def test_duplicate_registration_without_replace_fails_closed() -> None:
    reg = ExtensionRegistry()
    ext = register_corp_ner(reg, _extension(_ok))

    with pytest.raises(ValueError, match="already registered"):
        reg.register(ext.spec, lambda: ext)


# ---- health -----------------------------------------------------------------


async def test_health_calls_the_service_ready_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    status = await _extension(handler).health()

    assert seen == [f"{_BASE_URL}{READY_PATH}"]
    assert status.healthy is True


async def test_health_unhealthy_on_non_2xx() -> None:
    status = await _extension(lambda request: httpx.Response(503)).health()

    assert status.healthy is False
    assert "503" in status.detail


async def test_health_unhealthy_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    status = await _extension(handler).health()

    assert status.healthy is False
    # Exception TYPE only — a transport message may name internal hosts.
    assert "ConnectError" in status.detail
    assert "no route to host" not in status.detail


# ---- /healthz/extensions ----------------------------------------------------


async def test_healthz_extensions_reports_corp_ner() -> None:
    reg = ExtensionRegistry()
    register_corp_ner(reg, _extension(_ok))

    report = await reg.health_all()
    assert "detector:corp_ner" in report
    assert report["detector:corp_ner"].healthy is True

    status = await ExtensionsCheck(health_all=reg.health_all).check()
    assert status.healthy is True


async def test_healthz_extensions_degrades_when_corp_ner_is_down() -> None:
    reg = ExtensionRegistry()
    register_corp_ner(reg, _extension(lambda request: httpx.Response(500)))

    status = await ExtensionsCheck(health_all=reg.health_all).check()

    assert status.healthy is False
    assert "detector:corp_ner" in status.detail


def test_extension_is_an_extension_subclass() -> None:
    assert isinstance(_extension(_ok), Extension)
