from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

# A string only NER catches (RU + EN persons) — no rule/regex/gazetteer would
# fire on it, so a finding proves the model actually loaded and inference ran.
_NER_PROBE_TEXT = "John Smith met Анна Кузнецова in Moscow"


@dataclass(frozen=True)
class HealthStatus:
    healthy: bool
    detail: str = ""


class HealthCheck(ABC):
    @abstractmethod
    async def check(self) -> HealthStatus: ...


class LiveCheck(HealthCheck):
    """`/healthz/live` — process is alive. Always returns healthy unless
    the asyncio loop itself is dead (in which case we wouldn't run)."""

    async def check(self) -> HealthStatus:
        return HealthStatus(healthy=True, detail="live")


class ReadyCheck(HealthCheck):
    """`/healthz/ready` — gateway can serve.

    Per the plan (M4-1): readiness checks Redis + Postgres. It does NOT
    check the corp LLM — corp-LLM unavailability is a request-level
    fail-closed (M4-3), not a readiness signal. Otherwise a flapping
    corp LLM would yo-yo us in/out of the load balancer.

    When ``CORP_LLM_REQUIRE_NER`` is set, an ``check_ner`` probe is also wired
    (see ``make_ner_ready_probe``). Unlike the corp LLM, NER-loaded is a static
    deploy property, not a flapping dependency: a pod started without the
    required NER model would 503 every request (F2 fail-closed), so it must fall
    out of the load balancer rather than serve.
    """

    def __init__(
        self,
        check_redis: Callable[[], Awaitable[bool]],
        check_postgres: Callable[[], Awaitable[bool]],
        *,
        check_ner: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._check_redis = check_redis
        self._check_postgres = check_postgres
        self._check_ner = check_ner

    async def check(self) -> HealthStatus:
        try:
            redis_ok = await self._check_redis()
        except Exception as exc:
            return HealthStatus(False, f"redis_error:{type(exc).__name__}")
        if not redis_ok:
            return HealthStatus(False, "redis_unhealthy")
        try:
            pg_ok = await self._check_postgres()
        except Exception as exc:
            return HealthStatus(False, f"postgres_error:{type(exc).__name__}")
        if not pg_ok:
            return HealthStatus(False, "postgres_unhealthy")
        if self._check_ner is not None:
            try:
                ner_ok = await self._check_ner()
            except Exception as exc:
                # A required-but-missing engine raises NerUnavailableError here.
                return HealthStatus(False, f"ner_error:{type(exc).__name__}")
            if not ner_ok:
                return HealthStatus(False, "ner_unhealthy")
        return HealthStatus(True, "ready")


def make_ner_ready_probe(
    detect: Callable[[str], Awaitable[Sequence[object]]],
    *,
    probe_text: str = _NER_PROBE_TEXT,
) -> Callable[[], Awaitable[bool]]:
    """Build a readiness probe confirming NER is actually loaded (F2/M4).

    Healthy iff the probe text yields >=1 finding — proof the model loaded and
    inference ran. A self-disabled required engine raises ``NerUnavailableError``,
    which ``ReadyCheck`` surfaces as ``ner_error``. Wire this into ``ReadyCheck``
    only when ``CORP_LLM_REQUIRE_NER`` is set; otherwise readiness stays on the
    dev graceful path (no NER probe).
    """

    async def _probe() -> bool:
        findings = await detect(probe_text)
        return len(findings) > 0

    return _probe


class SanitizationCheck(HealthCheck):
    """`/healthz/sanitization` — deep-check, separate from k8s probes.

    Runs a synthetic redactable string end-to-end and asserts the round
    trip succeeds. Not wired to readiness because: corp LLM may be down
    (handled at request time), and we don't want every probe to spend
    a corp-LLM token call.
    """

    def __init__(self, run_round_trip: Callable[[], Awaitable[bool]]) -> None:
        self._run = run_round_trip

    async def check(self) -> HealthStatus:
        try:
            ok = await self._run()
        except Exception as exc:
            return HealthStatus(False, f"sanitization_error:{type(exc).__name__}")
        return HealthStatus(ok, "sanitization_ok" if ok else "sanitization_failed")


class ExtensionsCheck(HealthCheck):
    """`/healthz/extensions` — deep-check aggregating registered-extension health.

    Like `SanitizationCheck`, this is a deep-check and is deliberately NOT wired
    into `/healthz/ready`: a flapping audit sink or tracing exporter is
    observability, not a serve/don't-serve signal, so it must never yo-yo the
    pod out of the load balancer (M4 fail-policy matrix).

    Healthy iff every registered extension reports healthy. The detail names the
    unhealthy extensions and flags `degraded` (some down) vs `unhealthy` (all
    down); an empty registry is healthy.
    """

    def __init__(self, health_all: Callable[[], Awaitable[dict[str, HealthStatus]]]) -> None:
        self._health_all = health_all

    async def check(self) -> HealthStatus:
        try:
            report = await self._health_all()
        except Exception as exc:
            return HealthStatus(False, f"extensions_error:{type(exc).__name__}")
        if not report:
            return HealthStatus(True, "no_extensions")
        down = sorted(name for name, status in report.items() if not status.healthy)
        if not down:
            return HealthStatus(True, f"extensions_ok:{len(report)}")
        label = "unhealthy" if len(down) == len(report) else "degraded"
        return HealthStatus(False, f"{label}: {', '.join(down)}")
