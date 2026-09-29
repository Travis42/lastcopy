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
import os
import random
from dataclasses import dataclass

import httpx

from . import JITTER_MAX, MIN_INTERVAL_PER_HOST, USER_AGENT
from .store import Store, host_of


def force_ipv4_from_env() -> bool:
    """LASTCOPY_FORCE_IPV4=1 -> bind local_address=0.0.0.0 (IPv4 egress only).

    Needed when an API key is IP-restricted to the host's IPv4 address but the
    host egresses IPv6 by default (e.g. the M3.1 Google Books key). Off by default.
    """
    return os.environ.get("LASTCOPY_FORCE_IPV4", "").strip().lower() in ("1", "true", "yes", "on")


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
        force_ipv4: bool | None = None,
    ):
        self.store = store
        self.min_interval = min_interval
        self.jitter_max = jitter_max
        self.sleep = sleep_fn
        self.rng = rng or random.Random()
        if force_ipv4 is None:
            force_ipv4 = force_ipv4_from_env()
        self.force_ipv4 = force_ipv4
        # httpx 0.28: local_address lives on the transport, not the client.
        # With an injected transport (tests) IPv4 forcing is a no-op.
        if transport is None and force_ipv4:
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
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
