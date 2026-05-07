import threading


class QuotaTracker:
    """Per-team byte budget for Cache A.

    Thread-safe in case of multi-worker pods. `try_consume` returns True
    on success and False if the request would exceed the team's limit.
    `release` returns bytes when an entry is evicted/expired.
    """

    def __init__(self, default_limit_bytes: int) -> None:
        if default_limit_bytes < 0:
            raise ValueError("default_limit_bytes must be >= 0")
        self._default_limit = default_limit_bytes
        self._limits: dict[str, int] = {}
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def set_team_limit(self, team_id: str, limit_bytes: int) -> None:
        if limit_bytes < 0:
            raise ValueError("limit_bytes must be >= 0")
        with self._lock:
            self._limits[team_id] = limit_bytes

    def used(self, team_id: str) -> int:
        with self._lock:
            return self._used.get(team_id, 0)

    def limit(self, team_id: str) -> int:
        with self._lock:
            return self._limits.get(team_id, self._default_limit)

    def try_consume(self, team_id: str, bytes_size: int) -> bool:
        if bytes_size < 0:
            raise ValueError("bytes_size must be >= 0")
        with self._lock:
            limit = self._limits.get(team_id, self._default_limit)
            current = self._used.get(team_id, 0)
            if current + bytes_size > limit:
                return False
            self._used[team_id] = current + bytes_size
            return True

    def release(self, team_id: str, bytes_size: int) -> None:
        if bytes_size < 0:
            raise ValueError("bytes_size must be >= 0")
        with self._lock:
            current = self._used.get(team_id, 0)
            self._used[team_id] = max(0, current - bytes_size)
