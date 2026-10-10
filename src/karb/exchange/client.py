"""The Kalshi REST client, built for a lossy network.

Measured from the development machine (docs/data-caveats.md): connections to the API are
often reset before a response arrives -- in some bursts, most requests fail that way -- while
rate limiting was never observed. So retries are a core feature, not an afterthought. A failed
GET is simply sent again after a jittered exponential backoff.

Market data is public. Portfolio requests -- balance, positions, fills, orders -- are signed
(``karb.trading.auth``) and go to Kalshi's demo exchange only: the client refuses to sign for
any other host (docs/decisions/ADR-0010).

An order POST is not idempotent by nature, so it is retried only with an identical body. Every
order carries a ``client_order_id``; a copy the exchange has already accepted comes back as a
409 (``KalshiConflict``) instead of a second order, and the caller looks up the original.

Reads and writes have separate token buckets, as Kalshi meters them separately. The read rate is
conservative because Kalshi publishes no unauthenticated limits. The write bucket counts orders:
Kalshi's Basic tier allows 100 write tokens a second at 10 tokens an order. A 429 -- which Kalshi
sends without a Retry-After header -- backs off like any other transient failure.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from karb import __version__
from karb.core.clock import Clock, SystemClock
from karb.trading.auth import DEMO_HOSTS, PRODUCTION_HOSTS, Credentials, CredentialsError
from karb.wire.decode import load_json

__all__ = [
    "DEFAULT_BASE_URL",
    "WRITE_RETRY",
    "ClientStats",
    "KalshiClient",
    "KalshiConflict",
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

    def __init__(self, status: int, path: str, body: str, method: str = "GET") -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")
        self.status = status
        self.path = path
        self.body = body


class KalshiConflict(KalshiHTTPError):
    """HTTP 409: for an order, a ``client_order_id`` the exchange has already accepted."""


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


WRITE_RETRY: Final = RetryPolicy(max_attempts=4, base_delay=0.25, max_delay=2.0)
"""Orders are retried briefly: an immediate-or-cancel order that arrives a minute late is a
different order."""


class TokenBucket:
    """Continuous-refill token bucket: ``rate`` tokens per second, bursts up to ``capacity``."""

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

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self._capacity:
            raise ValueError(f"a request costing {cost} can never fit a bucket of {self._capacity}")
        async with self._lock:
            while True:
                now = self._clock.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await self._sleep((cost - self._tokens) / self._rate)


class KalshiClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        rate: float = 8.0,
        burst: float = 8.0,
        write_rate: float = 10.0,
        write_burst: float = 10.0,
        retry: RetryPolicy | None = None,
        write_retry: RetryPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        rng: random.Random | None = None,
        credentials: Credentials | None = None,
        sign_hosts: frozenset[str] = DEMO_HOSTS,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self._sleep: Sleep = sleep or asyncio.sleep
        self._retry = retry or RetryPolicy()
        self._write_retry = write_retry or WRITE_RETRY
        self._rng = rng or random.Random()
        self._bucket = TokenBucket(rate, burst, self.clock, self._sleep)
        self._write_bucket = TokenBucket(write_rate, write_burst, self.clock, self._sleep)
        self._credentials = credentials
        self._sign_hosts = sign_hosts
        purpose = "demo trading" if credentials is not None else "public data only"
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": f"karb/{__version__} (research scanner; {purpose})",
            },
        )
        self.stats = ClientStats()

    @property
    def base_url(self) -> str:
        return str(self._http.base_url)

    @property
    def authenticated(self) -> bool:
        return self._credentials is not None

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def pause(self, seconds: float) -> None:
        """Wait on the client's own clock, so tests with a fake clock stay instant."""
        await self._sleep(seconds)

    async def get(self, path: str, params: Params | None = None, *, auth: bool = False) -> Any:
        """GET ``path`` and decode the JSON body, retrying transient failures."""
        return await self._send("GET", path, params=params, auth=auth)

    async def post(self, path: str, body: Mapping[str, Any], *, cost: int = 1) -> Any:
        """A signed POST, metered as ``cost`` orders. Retried only with this exact body."""
        return await self._send("POST", path, body=body, auth=True, cost=cost)

    async def delete(self, path: str) -> Any:
        """A signed DELETE, metered as one write."""
        return await self._send("DELETE", path, auth=True, cost=1)

    def _signed_headers(self, request: httpx.Request) -> dict[str, str]:
        if self._credentials is None:
            raise CredentialsError("this client has no credentials for portfolio requests")
        host = request.url.host
        if host in PRODUCTION_HOSTS or host not in self._sign_hosts:
            raise CredentialsError(
                f"refusing to sign a request to {host}: karb trades on Kalshi's demo exchange "
                "only, with mock funds"
            )
        path = request.url.raw_path.decode("ascii")
        timestamp_ms = int(self.clock.now().timestamp() * 1000)
        return self._credentials.headers(timestamp_ms, request.method, path)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        body: Mapping[str, Any] | None = None,
        auth: bool,
        cost: int = 1,
    ) -> Any:
        write = method != "GET"
        retry = self._write_retry if write else self._retry
        attempt = 0
        while True:
            if write:
                await self._write_bucket.acquire(cost)
            else:
                await self._bucket.acquire()
            request = self._http.build_request(
                method,
                path,
                params=None if params is None else tuple(params),
                json=None if body is None else dict(body),
            )
            if auth:
                request.headers.update(self._signed_headers(request))
            self.stats.requests += 1
            failure: str
            try:
                response = await self._http.send(request)
            except httpx.TransportError as exc:
                self.stats.resets += 1
                failure = f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if status in (200, 201, 204):
                    if not response.content:
                        return {}
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
                elif status == 409:
                    raise KalshiConflict(status, path, response.text[:300], method)
                else:
                    raise KalshiHTTPError(status, path, response.text[:300], method)
            attempt += 1
            if attempt >= retry.max_attempts:
                self.stats.failures += 1
                raise KalshiUnavailable(f"{method} {path} failed {attempt} times; last: {failure}")
            self.stats.retries += 1
            await self._sleep(retry.delay(attempt, self._rng))

    async def paginate(
        self,
        path: str,
        params: Params,
        item_key: str,
        *,
        max_pages: int | None = None,
        auth: bool = False,
    ) -> AsyncIterator[list[Any]]:
        """Yield each page's ``item_key`` list, following Kalshi's opaque cursors."""
        cursor: str | None = None
        pages = 0
        while True:
            query = [*params, ("cursor", cursor)] if cursor else list(params)
            payload = await self.get(path, query, auth=auth)
            yield list(payload.get(item_key) or [])
            pages += 1
            cursor = payload.get("cursor") or None
            if cursor is None or (max_pages is not None and pages >= max_pages):
                return
