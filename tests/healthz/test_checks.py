import pytest

from corp_llm_gateway.healthz import (
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
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


def test_status_is_immutable_dataclass() -> None:
    s = HealthStatus(True)
    with pytest.raises(Exception):
        s.healthy = False  # type: ignore[misc]
