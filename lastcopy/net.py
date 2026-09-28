"""Polite async HTTP layer: 1 req/s/host + jitter, mandatory UA, TTL cache, rate ledger.

Every real network request goes through PoliteClient.get() which:
  * consults the SQLite cache first (TTL 30d; cache hits skip network + ledger),
  * enforces MIN_INTERVAL_PER_HOST (+ uniform jitter) via the ratelimit_ledger,
  * logs every network attempt to the ratelimit_ledger,
  * never raises on transport/HTTP errors — returns a normalized envelope so
    source-down / 429 / empty bodies become status=unavailable downstream.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

from . import JITTER_MAX, MIN_INTERVAL_PER_HOST, USER_AGENT
from .store import Store, host_of


@dataclass
class NormResponse:
    status: int
    body: str
    ok: bool  # 2xx and non-empty body
    from_cache: bool = False


class PoliteClient:
    def __init__(
        self,
        store: Store,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = MIN_INTERVAL_PER_HOST,
        jitter_max: float = JITTER_MAX,
        sleep_fn=asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.store = store
        self.min_interval = min_interval
        self.jitter_max = jitter_max
        self.sleep = sleep_fn
        self.rng = rng or random.Random()
        self.client = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def throttle(self, host: str) -> None:
        """Sleep just long enough that consecutive requests to `host` are >= min_interval apart."""
        import time

        last = self.store.last_request_ts(host)
        if last is not None:
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                await self.sleep(wait + self.rng.uniform(0, self.jitter_max))

    async def get(self, url: str, params: dict | None = None) -> NormResponse:
        host = host_of(url)
        cached = self.store.cache_get(url, params)
        if cached is not None:
            status, body = cached
            return NormResponse(status=status, body=body, ok=_ok(status, body), from_cache=True)

        await self.throttle(host)
        try:
            resp = await self.client.get(url, params=params)
            status, body = resp.status_code, resp.text
            self.store.log_request(host, url, f"http:{status}")
        except httpx.HTTPError as exc:
            status, body = 0, ""
            self.store.log_request(host, url, f"error:{type(exc).__name__}")
        self.store.cache_put(url, params, status, body)
        return NormResponse(status=status, body=body, ok=_ok(status, body))


def _ok(status: int, body: str) -> bool:
    return 200 <= status < 300 and bool(body.strip())
