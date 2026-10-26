"""Per-client limit on failed web panel logins (sliding window, in memory, per process)."""

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

DEFAULT_MAX_FAILURES = 10
DEFAULT_WINDOW_SECONDS = 15 * 60.0
DEFAULT_MAX_CLIENTS = 10_000


class LoginThrottle:
    """Block a client after ``max_failures`` wrong logins within ``window_seconds``.

    A blocked client is allowed again as soon as its oldest failure leaves the window, so
    a client can never make more than ``max_failures`` wrong guesses per window. State is
    kept per process and lost on restart; run ``serve`` with a single worker.
    Memory is bounded: at most ``max_clients`` clients are tracked (least recent dropped).
    """

    def __init__(
        self,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_clients: int = DEFAULT_MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures < 1 or window_seconds <= 0 or max_clients < 1:
            msg = "max_failures, window_seconds and max_clients must be positive"
            raise ValueError(msg)
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, client: str, now: float) -> deque[float] | None:
        failures = self._failures.get(client)
        if failures is None:
            return None
        while failures and failures[0] <= now - self.window_seconds:
            failures.popleft()
        if not failures:
            del self._failures[client]
            return None
        return failures

    def retry_after(self, client: str) -> int | None:
        """Seconds until ``client`` may try again, or None if it is not blocked."""
        with self._lock:
            now = self._clock()
            failures = self._recent(client, now)
            if failures is None or len(failures) < self.max_failures:
                return None
            return max(1, math.ceil(failures[0] + self.window_seconds - now))

    def record_failure(self, client: str) -> bool:
        """Count a wrong login; returns True when this failure blocks the client."""
        with self._lock:
            now = self._clock()
            failures = self._recent(client, now)
            if failures is None:
                failures = self._failures[client] = deque()
            failures.append(now)
            self._failures.move_to_end(client)
            while len(self._failures) > self._max_clients:
                self._failures.popitem(last=False)
            return len(failures) == self.max_failures

    def reset(self, client: str) -> None:
        """Forget a client's failures (after a successful login)."""
        with self._lock:
            self._failures.pop(client, None)
