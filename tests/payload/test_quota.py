import threading

import pytest

from corp_llm_gateway.payload import QuotaTracker


def test_negative_default_limit_rejected() -> None:
    with pytest.raises(ValueError):
        QuotaTracker(default_limit_bytes=-1)


def test_consume_within_default_limit() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    assert tracker.try_consume("team-a", 500) is True
    assert tracker.used("team-a") == 500


def test_consume_exceeding_default_limit_rejected() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    assert tracker.try_consume("team-a", 600) is True
    assert tracker.try_consume("team-a", 500) is False
    assert tracker.used("team-a") == 600


def test_consume_exactly_at_limit() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    assert tracker.try_consume("team-a", 1000) is True
    assert tracker.try_consume("team-a", 1) is False


def test_per_team_isolation() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    assert tracker.try_consume("team-a", 1000) is True
    assert tracker.try_consume("team-b", 1000) is True
    assert tracker.used("team-a") == 1000
    assert tracker.used("team-b") == 1000


def test_team_specific_limit_overrides_default() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    tracker.set_team_limit("team-a", 500)
    assert tracker.try_consume("team-a", 600) is False
    assert tracker.try_consume("team-a", 500) is True


def test_release_reduces_used() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    tracker.try_consume("team-a", 800)
    tracker.release("team-a", 300)
    assert tracker.used("team-a") == 500


def test_release_floor_at_zero() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    tracker.try_consume("team-a", 100)
    tracker.release("team-a", 999)
    assert tracker.used("team-a") == 0


def test_negative_consume_rejected() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    with pytest.raises(ValueError):
        tracker.try_consume("team-a", -1)


def test_concurrent_consume_race_safe() -> None:
    tracker = QuotaTracker(default_limit_bytes=1000)
    successes: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        ok = tracker.try_consume("team-a", 100)
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for x in successes if x) == 10
    assert tracker.used("team-a") == 1000
