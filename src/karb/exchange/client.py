"""The Kalshi REST client: public market data only, built for a lossy network.

Measured from the development machine (docs/data-caveats.md): connections to the API are
often reset before a response arrives -- in some bursts, most requests fail that way -- while
rate limiting was never observed. So retries are a core feature, not an afterthought. Every
request is a GET, GETs are idempotent, and a failed one is simply sent again after a jittered
exponential backoff.

One token bucket paces every request. Kalshi documents budgets only for authenticated
traffic and publishes no unauthenticated limits, so the default rate is conservative, and a
429 -- which Kalshi sends without a Retry-After header -- backs off like any other transient
failure.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from karb import __version__
from karb.core.clock import Clock, SystemClock
from karb.wire.decode import load_json

__all__ = [
    "DEFAULT_BASE_URL",
    "ClientStats",
    "KalshiClient",
    "KalshiError",
    "KalshiHTTPError",
    "KalshiUnavailable",
    "Params",
    "RetryPolicy",
    "TokenBucket",
]

DEFAULT_BASE_URL: Final = "https://api.elections.kalshi.com/trade-api/v2"

Params = Sequence[tuple[str, str | int]]
Sleep = Callable[[float], Awaitable[None]]


class KalshiError(Exception):
    """Base class for client failures."""


class KalshiHTTPError(KalshiError):
    """A non-retryable HTTP status (a 4xx other than 429)."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"GET {path} -> HTTP {status}: {body}")
        self.status = status
        self.path = path


class KalshiUnavailable(KalshiError):
    """Every retry of a request failed."""


@dataclass
class ClientStats:
    requests: int = 0
    """Attempts sent, retries included."""
    retries: int = 0
    resets: int = 0
    """Transport failures: resets, timeouts, truncated bodies."""
    throttled: int = 0
    server_errors: int = 0
    failures: int = 0
    """Requests abandoned after exhausting every retry."""
    bytes_received: int = 0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 10
    base_delay: float = 0.25
    max_delay: float = 8.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Full-jitter exponential backoff before retry number ``attempt`` (1-based)."""
        return rng.uniform(0.0, min(self.max_delay, self.base_delay * 2**attempt))


class TokenBucket:
    """Continuous-refill token bucket: ``rate`` requests per second, bursts up to ``capacity``."""

    def __init__(self, rate: float, capacity: float, clock: Clock, sleep: Sleep) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be positive and capacity at least 1")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)


class KalshiClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        rate: float = 8.0,
        burst: float = 8.0,
        retry: RetryPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self._sleep: Sleep = sleep or asyncio.sleep
        self._retry = retry or RetryPolicy()
        self._rng = rng or random.Random()
        self._bucket = TokenBucket(rate, burst, self.clock, self._sleep)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": f"karb/{__version__} (research scanner; public data only)",
            },
        )
        self.stats = ClientStats()

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: Params | None = None) -> Any:
        """GET ``path`` and decode the JSON body, retrying transient failures."""
        attempt = 0
        while True:
            await self._bucket.acquire()
            self.stats.requests += 1
            failure: str
            try:
                response = await self._http.get(
                    path, params=None if params is None else tuple(params)
                )
            except httpx.TransportError as exc:
                self.stats.resets += 1
                failure = f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        payload = load_json(response.content)
                    except ValueError as exc:  # a body cut off mid-transfer
                        self.stats.resets += 1
                        failure = f"undecodable body: {exc}"
                    else:
                        self.stats.bytes_received += len(response.content)
                        return payload
                elif status == 429:
                    self.stats.throttled += 1
                    failure = "HTTP 429"
                elif status >= 500:
                    self.stats.server_errors += 1
                    failure = f"HTTP {status}"
                else:
                    raise KalshiHTTPError(status, path, response.text[:300])
            attempt += 1
            if attempt >= self._retry.max_attempts:
                self.stats.failures += 1
                raise KalshiUnavailable(f"GET {path} failed {attempt} times; last: {failure}")
            self.stats.retries += 1
            await self._sleep(self._retry.delay(attempt, self._rng))

    async def paginate(
        self,
        path: str,
        params: Params,
        item_key: str,
        *,
        max_pages: int | None = None,
    ) -> AsyncIterator[list[Any]]:
        """Yield each page's ``item_key`` list, following Kalshi's opaque cursors."""
        cursor: str | None = None
        pages = 0
        while True:
            query = [*params, ("cursor", cursor)] if cursor else list(params)
            payload = await self.get(path, query)
            yield list(payload.get(item_key) or [])
            pages += 1
            cursor = payload.get("cursor") or None
            if cursor is None or (max_pages is not None and pages >= max_pages):
                return
