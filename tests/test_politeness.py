"""Politeness + cache + resume: rate ledger enforcement, cache-hit skip, crash resumability."""

import asyncio

import httpx

from lastcopy.net import PoliteClient
from lastcopy.store import Store
from tests.conftest import Router, load


def run(coro):
    return asyncio.run(coro)


def test_rate_ledger_enforces_min_interval_per_host(make_client, recorder, store):
    r = Router().add_default("ol_search_miss")
    client = make_client(r, min_interval=1.0)
    run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    assert recorder.sleeps == []  # first request: no wait needed
    run(client.get("https://openlibrary.org/search.json", {"q": "b"}))
    assert recorder.sleeps, "second request to same host must sleep"
    assert recorder.sleeps[0] >= 1.0  # >= min_interval (jitter adds on top)


def test_rate_ledger_jitter_added(make_client, recorder, store):
    r = Router().add_default("ol_search_miss")
    client = make_client(r, min_interval=1.0)
    run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    run(client.get("https://openlibrary.org/search.json", {"q": "b"}))
    assert recorder.sleeps[0] >= 1.0
    assert recorder.sleeps[0] <= 1.25  # min_interval + max jitter


def test_independent_hosts_do_not_block_each_other(make_client, recorder, store):
    r = Router().add_default("ia_miss")
    client = make_client(r, min_interval=1.0)
    run(client.get("https://archive.org/advancedsearch.php", {"q": "a"}))
    run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    assert recorder.sleeps == []  # different host: no throttle


def test_every_network_request_logged_in_ledger(make_client, store):
    r = Router().add_default("ol_search_miss")
    client = make_client(r)
    run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    run(client.get("https://openlibrary.org/search.json", {"q": "b"}))
    rows = store.ledger_for_host("openlibrary.org")
    assert len(rows) == 2
    assert rows[0]["ts"] <= rows[1]["ts"]


def test_cache_hit_skips_network_and_throttle(make_client, recorder, store):
    r = Router().add_default("ol_search_miss")
    client = make_client(r)
    first = run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    n_reqs = len(r.requests)
    assert not first.from_cache
    second = run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    assert second.from_cache
    assert len(r.requests) == n_reqs          # no new network request
    assert recorder.sleeps == []              # and no throttle sleep
    assert len(store.ledger_for_host("openlibrary.org")) == 1  # ledger unchanged


def test_cache_keyed_on_url_plus_params(make_client, store):
    r = Router().add_default("ol_search_miss")
    client = make_client(r)
    run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    run(client.get("https://openlibrary.org/search.json", {"q": "different"}))
    assert len(r.requests) == 2  # different params = different cache key = real request


def test_cache_ttl_expiry(tmp_path, make_client, store):
    import time
    r = Router().add_default("ol_search_miss")
    client = make_client(r)
    run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    store.conn.execute("UPDATE cache SET stored_at = ?", (time.time() - 31 * 86400,))
    store.conn.commit()
    again = run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    assert not again.from_cache  # 30d TTL expired -> refetch


def test_transport_error_is_unavailable_not_crash(store, recorder):
    def boom(request):
        raise httpx.ConnectError("boom")

    client = PoliteClient(store, transport=httpx.MockTransport(boom),
                          sleep_fn=recorder)
    resp = run(client.get("https://openlibrary.org/search.json", {"q": "a"}))
    assert resp.status == 0 and not resp.ok
    rows = store.ledger_for_host("openlibrary.org")
    assert rows and rows[0]["status"].startswith("error:")
    run(client.aclose())


def test_resume_after_crash_only_pending_remain(tmp_path):
    from lastcopy.models import Edition
    store = Store(tmp_path / "resume.db")
    store.upsert_edition(Edition(work_key="9780140328721", isbn13="9780140328721"))
    store.upsert_edition(Edition(work_key="9780060935467", isbn13="9780060935467"))
    for wk in ("9780140328721", "9780060935467"):
        for src in ("ol", "ia", "wd"):
            store.enqueue(wk, src)
    # crash mid-run: one item finished, rest untouched
    store.queue_mark("9780140328721", "ol", "done")
    pending = store.pending("ol")
    assert [row["work_key"] for row in pending] == ["9780060935467"]
    # re-running enqueue does not resurrect done rows
    store.enqueue("9780140328721", "ol")
    assert [row["work_key"] for row in store.pending("ol")] == ["9780060935467"]
    # but a failed row stays pending for retry (attempts tracked)
    store.queue_mark("9780060935467", "ia", "pending", "timeout")
    ia_rows = {r["work_key"]: r for r in store.pending("ia")}
    assert ia_rows["9780060935467"]["attempts"] == 1
    store.close()
