"""Shared test fixtures: tmp SQLite store + shape-faithful httpx MockTransport from files."""

from __future__ import annotations

import json
import pathlib
import re

import httpx
import pytest

from lastcopy import USER_AGENT
from lastcopy.net import PoliteClient
from lastcopy.store import Store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


class Router:
    """Route a request to a fixture file by inspecting url + query params."""

    def __init__(self):
        self.routes: list[tuple[callable, str | tuple[int, str]]] = []
        self.requests: list[httpx.Request] = []

    def add(self, predicate, body: str | tuple[int, str]):
        self.routes.append((predicate, body))
        return self

    def add_q_contains(self, needle: str, fixture: str, status: int = 200, host: str | None = None):
        def predicate(req, params):
            if host and req.url.host != host:
                return False
            return str(params.get("q", "")).find(needle) >= 0
        return self.add(predicate, (status, load(fixture)))

    def add_url_contains(self, needle: str, fixture: str, status: int = 200, host: str | None = None):
        def predicate(req, params):
            if host and req.url.host != host:
                return False
            return needle in str(req.url)
        return self.add(predicate, (status, load(fixture)))

    def add_default(self, fixture: str, status: int = 200, host: str | None = None):
        def predicate(req, params):
            return host is None or req.url.host == host
        return self.add(predicate, (status, load(fixture)))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(httpx.QueryParams(request.url.query))
        for predicate, body in self.routes:
            try:
                if predicate(request, params):
                    status, text = body if isinstance(body, tuple) else (200, body)
                    return httpx.Response(status, text=text,
                                          headers={"Content-Type": "application/json"})
            except Exception:
                continue
        return httpx.Response(404, text='{"no route": true}')

    def assert_polite_ua(self):
        assert all(r.headers.get("User-Agent") == USER_AGENT for r in self.requests)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class Recorder:
    """sleep_fn replacement: records requested delays instead of sleeping."""

    def __init__(self):
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float):
        self.sleeps.append(seconds)


@pytest.fixture()
def recorder():
    return Recorder()


@pytest.fixture()
def make_client(store, recorder):
    def _make(router: Router, min_interval: float = 1.0) -> PoliteClient:
        return PoliteClient(
            store,
            transport=httpx.MockTransport(router.handler),
            min_interval=min_interval,
            sleep_fn=recorder,
            rng=__import__("random").Random(42),
        )
    return _make
