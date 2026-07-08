from collections.abc import Awaitable, Callable

import pytest

from corp_llm_gateway.healthz import (
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
    make_ner_ready_probe,
)

# Live ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_always_healthy() -> None:
    assert (await LiveCheck().check()).healthy is True


# Ready ---------------------------------------------------------------------


async def _ok() -> bool:
    return True


async def _fail() -> bool:
    return False


async def _raise() -> bool:
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_ready_healthy_when_both_dependencies_ok() -> None:
    rc = ReadyCheck(check_redis=_ok, check_postgres=_ok)
    status = await rc.check()
    assert status.healthy is True


@pytest.mark.asyncio
async def test_ready_unhealthy_when_redis_unhealthy() -> None:
    rc = ReadyCheck(check_redis=_fail, check_postgres=_ok)
    status = await rc.check()
    assert status.healthy is False
    assert "redis" in status.detail


@pytest.mark.asyncio
async def test_ready_unhealthy_when_postgres_unhealthy() -> None:
    rc = ReadyCheck(check_redis=_ok, check_postgres=_fail)
    status = await rc.check()
    assert status.healthy is False
    assert "postgres" in status.detail


@pytest.mark.asyncio
async def test_ready_redis_exception_caught() -> None:
    rc = ReadyCheck(check_redis=_raise, check_postgres=_ok)
    status = await rc.check()
    assert status.healthy is False
    assert "redis_error" in status.detail


@pytest.mark.asyncio
async def test_ready_postgres_exception_caught() -> None:
    rc = ReadyCheck(check_redis=_ok, check_postgres=_raise)
    status = await rc.check()
    assert status.healthy is False
    assert "postgres_error" in status.detail


@pytest.mark.asyncio
async def test_ready_short_circuits_on_redis_failure() -> None:
    pg_called = False

    async def pg() -> bool:
        nonlocal pg_called
        pg_called = True
        return True

    await ReadyCheck(check_redis=_fail, check_postgres=pg).check()
    assert pg_called is False, "Postgres should not be checked once Redis fails"


# Ready — NER probe (F2/A2, wired only when CORP_LLM_REQUIRE_NER is set) ------


@pytest.mark.asyncio
async def test_ready_healthy_when_ner_probe_ok() -> None:
    rc = ReadyCheck(check_redis=_ok, check_postgres=_ok, check_ner=_ok)
    status = await rc.check()
    assert status.healthy is True
    assert status.detail == "ready"


@pytest.mark.asyncio
async def test_ready_unhealthy_when_ner_probe_reports_not_loaded() -> None:
    rc = ReadyCheck(check_redis=_ok, check_postgres=_ok, check_ner=_fail)
    status = await rc.check()
    assert status.healthy is False
    assert status.detail == "ner_unhealthy"


@pytest.mark.asyncio
async def test_ready_ner_unavailable_error_surfaces_as_unhealthy() -> None:
    from corp_llm_gateway.detectors import NerUnavailableError

    async def _ner_absent() -> bool:
        raise NerUnavailableError("NER required but unavailable: RuNerDetector")

    rc = ReadyCheck(check_redis=_ok, check_postgres=_ok, check_ner=_ner_absent)
    status = await rc.check()
    assert status.healthy is False
    assert "ner_error:NerUnavailableError" in status.detail


@pytest.mark.asyncio
async def test_ready_ner_probe_not_run_until_deps_pass() -> None:
    ner_called = False

    async def _ner() -> bool:
        nonlocal ner_called
        ner_called = True
        return True

    await ReadyCheck(check_redis=_fail, check_postgres=_ok, check_ner=_ner).check()
    assert ner_called is False, "NER probe should not run once Redis fails"


@pytest.mark.asyncio
async def test_ready_no_ner_probe_stays_on_dev_graceful_path() -> None:
    """require-ner off ⇒ no check_ner wired ⇒ readiness unchanged (dev path)."""
    rc = ReadyCheck(check_redis=_ok, check_postgres=_ok)
    assert (await rc.check()).healthy is True


@pytest.mark.asyncio
async def test_make_ner_ready_probe_healthy_on_finding() -> None:
    from corp_llm_gateway.detectors.base import Finding

    async def _detect(text: str) -> list[Finding]:
        return [Finding(text="John Smith", label="PERSON", start=0, end=10, score=0.8)]

    probe = make_ner_ready_probe(_detect)
    assert await probe() is True


@pytest.mark.asyncio
async def test_make_ner_ready_probe_unhealthy_on_no_finding() -> None:
    from corp_llm_gateway.detectors.base import Finding

    async def _detect(text: str) -> list[Finding]:
        return []

    probe = make_ner_ready_probe(_detect)
    assert await probe() is False


@pytest.mark.asyncio
async def test_make_ner_ready_probe_propagates_detector_error() -> None:
    from corp_llm_gateway.detectors import NerUnavailableError
    from corp_llm_gateway.detectors.base import Finding

    async def _detect(text: str) -> list[Finding]:
        raise NerUnavailableError("boom")

    probe = make_ner_ready_probe(_detect)
    with pytest.raises(NerUnavailableError):
        await probe()


# Sanitization deep-check ---------------------------------------------------


@pytest.mark.asyncio
async def test_sanitization_healthy_when_round_trip_ok() -> None:
    sc = SanitizationCheck(run_round_trip=_ok)
    status = await sc.check()
    assert status.healthy is True


@pytest.mark.asyncio
async def test_sanitization_unhealthy_when_round_trip_fails() -> None:
    sc = SanitizationCheck(run_round_trip=_fail)
    assert (await sc.check()).healthy is False


@pytest.mark.asyncio
async def test_sanitization_exception_caught() -> None:
    sc = SanitizationCheck(run_round_trip=_raise)
    status = await sc.check()
    assert status.healthy is False
    assert "sanitization_error" in status.detail


# Extensions deep-check -----------------------------------------------------


def _health_all(
    report: dict[str, HealthStatus],
) -> Callable[[], Awaitable[dict[str, HealthStatus]]]:
    async def _run() -> dict[str, HealthStatus]:
        return report

    return _run


@pytest.mark.asyncio
async def test_extensions_healthy_when_all_healthy() -> None:
    ec = ExtensionsCheck(
        health_all=_health_all(
            {
                "audit_sink:stdout": HealthStatus(True, "ok"),
                "metrics:prometheus": HealthStatus(True, "ok"),
            }
        )
    )
    status = await ec.check()
    assert status.healthy is True


@pytest.mark.asyncio
async def test_extensions_healthy_when_registry_empty() -> None:
    status = await ExtensionsCheck(health_all=_health_all({})).check()
    assert status.healthy is True
    assert "no_extensions" in status.detail


@pytest.mark.asyncio
async def test_extensions_degraded_names_only_the_unhealthy() -> None:
    ec = ExtensionsCheck(
        health_all=_health_all(
            {
                "audit_sink:stdout": HealthStatus(True, "ok"),
                "metrics:prometheus": HealthStatus(False, "scrape_failed"),
            }
        )
    )
    status = await ec.check()
    assert status.healthy is False
    assert "degraded" in status.detail
    assert "metrics:prometheus" in status.detail
    assert "audit_sink:stdout" not in status.detail


@pytest.mark.asyncio
async def test_extensions_unhealthy_when_all_unhealthy() -> None:
    ec = ExtensionsCheck(
        health_all=_health_all(
            {
                "audit_sink:stdout": HealthStatus(False, "down"),
                "metrics:prometheus": HealthStatus(False, "down"),
            }
        )
    )
    status = await ec.check()
    assert status.healthy is False
    assert "unhealthy" in status.detail


@pytest.mark.asyncio
async def test_extensions_exception_caught() -> None:
    async def _boom() -> dict[str, HealthStatus]:
        raise RuntimeError("registry blew up")

    status = await ExtensionsCheck(health_all=_boom).check()
    assert status.healthy is False
    assert "extensions_error" in status.detail


def test_status_is_immutable_dataclass() -> None:
    s = HealthStatus(True)
    with pytest.raises(AttributeError):
        s.healthy = False  # type: ignore[misc]
