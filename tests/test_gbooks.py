"""Google Books (M3.1): viewability matrix, key handling, unavailable normalization."""

import asyncio
import json
import subprocess

import pytest

from lastcopy.models import Edition, HitStatus, SurrogateAccess
from lastcopy.sources import gbooks
from tests.conftest import Router, load

ED = Edition(work_key="9780486280615", isbn13="9780486280615",
             title="Adventures of Huckleberry Finn", origin_note="csv")
NO_ISBN_ED = Edition(work_key="bib-x", title="T", author="A",
                     origin_note="bib-stub:no-isbn")

FAKE_KEY = "test-key-never-a-real-one"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setattr(gbooks, "resolve_key", lambda: FAKE_KEY)


def gb_router(fixture: str, isbn: str = "9780486280615", status: int = 200):
    r = Router()
    r.routes = [(lambda req, p: req.url.host == "www.googleapis.com"
                 and str(p.get("q", "")) == f"isbn:{isbn}",
                 (status, load(fixture)))]
    return r


# ------------------------------------------------------------- viewability matrix
def test_gb_full_pages_is_public_surrogate(make_client, store, with_key):
    ed = Edition(work_key="9780553213119", isbn13="9780553213119")
    hit, surrogates = run(gbooks.check(ed, make_client(gb_router("gb_full", "9780553213119")), store))
    assert hit.status is HitStatus.ok
    assert len(surrogates) == 1
    assert surrogates[0].provider == "gb"
    assert surrogates[0].access is SurrogateAccess.public
    assert "books.google" in surrogates[0].url


def test_gb_all_pages_is_public_surrogate(make_client, store, with_key):
    # real Google scans of public-domain books return ALL_PAGES (seen live 2026-09-29);
    # same meaning as FULL_PAGES -> public
    body = json.loads(load("gb_full"))
    assert body["items"][0]["accessInfo"]["viewability"] == "ALL_PAGES"
    hit, surrogates = run(gbooks.check(
        Edition(work_key="9780553213119", isbn13="9780553213119"),
        make_client(gb_router("gb_full", "9780553213119")), store))
    assert surrogates and surrogates[0].access is SurrogateAccess.public


def test_gb_partial_is_partial_not_public(make_client, store, with_key):
    hit, surrogates = run(gbooks.check(ED, make_client(gb_router("gb_partial")), store))
    assert hit.status is HitStatus.ok
    assert len(surrogates) == 1
    assert surrogates[0].access is SurrogateAccess.partial


def test_gb_sample_is_partial(make_client, store, with_key):
    body = json.loads(load("gb_partial"))
    body["items"][0]["accessInfo"]["viewability"] = "SAMPLE"
    r = gb_router("gb_partial")
    r.routes[0] = (r.routes[0][0], (200, json.dumps(body)))
    _, surrogates = run(gbooks.check(ED, make_client(r), store))
    assert surrogates and surrogates[0].access is SurrogateAccess.partial


def test_gb_no_pages_yields_no_surrogate(make_client, store, with_key):
    hit, surrogates = run(gbooks.check(
        Edition(work_key="9780140283334", isbn13="9780140283334"),
        make_client(gb_router("gb_no_pages", "9780140283334")), store))
    assert hit.status is HitStatus.ok
    assert surrogates == []
    assert hit.evidence_json["viewability"] == "NO_PAGES"
    # source-side title/author/year recorded (sample-verification evidence)
    assert hit.evidence_json["title"] == "Lord of the Flies"
    assert hit.evidence_json["authors"] == ["William Golding"]


def test_gb_absent_viewability_is_no_surrogate(make_client, store, with_key):
    body = json.loads(load("gb_no_pages"))
    del body["items"][0]["accessInfo"]["viewability"]
    r = gb_router("gb_no_pages", "9780140283334")
    r.routes[0] = (r.routes[0][0], (200, json.dumps(body)))
    _, surrogates = run(gbooks.check(
        Edition(work_key="9780140283334", isbn13="9780140283334"),
        make_client(r), store))
    assert surrogates == []


def test_gb_first_item_decides_all_identifiers_recorded(make_client, store, with_key):
    body = json.loads(load("gb_no_pages"))  # first item: NO_PAGES
    second = json.loads(json.dumps(body["items"][0]))
    second["id"] = "OTHERVOLUME99"
    second["accessInfo"]["viewability"] = "FULL_PAGES"
    body["items"].append(second)
    r = gb_router("gb_no_pages", "9780140283334")
    r.routes[0] = (r.routes[0][0], (200, json.dumps(body)))
    hit, surrogates = run(gbooks.check(
        Edition(work_key="9780140283334", isbn13="9780140283334"),
        make_client(r), store))
    assert surrogates == []  # first item (NO_PAGES) decides
    assert set(hit.evidence_json["identifiers"]) == {body["items"][0]["id"], "OTHERVOLUME99"}


def test_gb_miss_is_ok_zero(make_client, store, with_key):
    hit, surrogates = run(gbooks.check(
        Edition(work_key="9780060935467", isbn13="9780060935467"),
        make_client(gb_router("gb_miss", "9780060935467")), store))
    assert hit.status is HitStatus.ok
    assert surrogates == []
    assert hit.evidence_json["totalItems"] == 0


# ------------------------------------------------------- unavailable normalization
def test_gb_403_ip_restriction_is_unavailable(make_client, store, with_key):
    hit, surrogates = run(gbooks.check(
        Edition(work_key="9780140283334", isbn13="9780140283334"),
        make_client(gb_router("gb_403", "9780140283334", status=403)), store))
    assert hit.status is HitStatus.unavailable
    assert surrogates == []


def test_gb_429_quota_is_unavailable(make_client, store, with_key):
    hit, _ = run(gbooks.check(
        Edition(work_key="9780140328721", isbn13="9780140328721"),
        make_client(gb_router("gb_429", "9780140328721", status=429)), store))
    assert hit.status is HitStatus.unavailable


def test_gb_empty_body_is_unavailable(make_client, store, with_key):
    r = gb_router("gb_partial")
    r.routes[0] = (r.routes[0][0], (200, ""))
    hit, _ = run(gbooks.check(ED, make_client(r), store))
    assert hit.status is HitStatus.unavailable


def test_gb_unavailable_never_green(classify_module, store, with_key):
    # matrix: source down -> classification stays UNKNOWN, never silently GREEN
    from lastcopy.models import Classification, Cls
    c = classify_module.classify_edition(ED, [], None, {"gb": "unavailable"})
    assert c.cls is Cls.UNKNOWN


# --------------------------------------------------------------- key handling
def test_gb_check_without_key_raises(make_client, store, monkeypatch):
    monkeypatch.setattr(gbooks, "resolve_key", lambda: None)
    with pytest.raises(gbooks.KeyRequiredError, match="key"):
        run(gbooks.check(ED, make_client(Router()), store))


def test_gb_bib_stub_is_skipped(make_client, store, with_key):
    hit, surrogates = run(gbooks.check(NO_ISBN_ED, make_client(Router()), store))
    assert hit.status is HitStatus.skipped
    assert surrogates == []


def test_gb_key_resolution_env_first_then_file(monkeypatch, tmp_path):
    kf = tmp_path / "gbooks.key"
    monkeypatch.delenv(gbooks.KEY_ENV, raising=False)
    monkeypatch.setattr(gbooks, "KEY_FILE", kf)
    assert gbooks.resolve_key() is None  # no env, no file
    kf.write_text("  file-key \n")
    assert gbooks.resolve_key() == "file-key"
    monkeypatch.setenv(gbooks.KEY_ENV, "env-key")
    assert gbooks.resolve_key() == "env-key"


def test_gb_key_travels_as_param_never_in_ledger_or_cache_params(make_client, store, with_key):
    run(gbooks.check(ED, make_client(gb_router("gb_partial")), store))
    rows = store.ledger_for_host("www.googleapis.com")
    assert rows and "key=" not in rows[0]["url"]  # ledger logs the bare URL only
    cached = store.conn.execute(
        "SELECT params_json FROM cache WHERE url LIKE '%googleapis%'").fetchone()
    assert json.loads(cached["params_json"])["key"] == "<redacted>"
    assert store.cache_get(gbooks.VOLUMES_URL,
                           {"q": "isbn:9780486280615", "key": FAKE_KEY}) is not None


def test_polite_ua_on_gb_requests(make_client, store, with_key):
    r = gb_router("gb_partial")
    run(gbooks.check(ED, make_client(r), store))
    r.assert_polite_ua()


# ------------------------------------------------------------- IPv4 knob (M3.1)
def test_force_ipv4_env_knob(monkeypatch):
    from lastcopy.net import force_ipv4_from_env
    monkeypatch.delenv("LASTCOPY_FORCE_IPV4", raising=False)
    assert force_ipv4_from_env() is False
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("LASTCOPY_FORCE_IPV4", v)
        assert force_ipv4_from_env() is True
    monkeypatch.setenv("LASTCOPY_FORCE_IPV4", "0")
    assert force_ipv4_from_env() is False


def test_polite_client_force_ipv4_flag(store):
    from lastcopy.net import PoliteClient
    client = PoliteClient(store, force_ipv4=True, min_interval=0.0)
    assert client.force_ipv4 is True
    run(client.aclose())


# ------------------------------------------------------------- key-leak scan
def test_no_google_api_key_committed_anywhere():
    """SPEC M3.1 hard rule: no tracked file may contain the key.

    Scans for Google-key-shaped material (AIza prefix + key body). The bare
    prefix alone can false-positive on prose (SPEC.md itself says "AIza"),
    so we match the full key shape: prefix + >=20 word/dash/underscore chars.
    """
    import pathlib
    import re
    key_re = re.compile(r"AIza[0-9A-Za-z_-]{20,}")
    root = pathlib.Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=str(root),
    ).stdout.split()
    assert tracked, "git ls-files returned nothing — wrong cwd?"
    leaks = []
    for path in tracked:
        try:
            blob = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if key_re.search(blob):
            leaks.append(path)
    assert not leaks, f"Google API key material found in tracked files: {leaks}"


# --------------------------------------------------------------- classify wiring
@pytest.fixture()
def classify_module():
    from lastcopy import classify
    return classify


def test_gb_public_surrogate_makes_green(classify_module):
    from lastcopy.models import Surrogate
    c = classify_module.classify_edition(ED, [
        Surrogate(work_key=ED.work_key, provider="gb",
                  access=SurrogateAccess.public, identifier="X", url="https://books.google.com/books?id=X")
    ], None, {"gb": "ok"})
    assert c.cls.value == "GREEN"
    assert "books.google.com" in c.evidence_urls[0]


def test_gb_partial_surrogate_is_not_green(classify_module):
    from lastcopy.models import Surrogate
    c = classify_module.classify_edition(ED, [
        Surrogate(work_key=ED.work_key, provider="gb",
                  access=SurrogateAccess.partial, identifier="X", url="https://books.google.com/books?id=X")
    ], None, {"gb": "ok", "ol": "ok", "ia": "ok", "wd": "ok"})
    assert c.cls.value == "RED-UNVERIFIED"  # partial is not an accessible surrogate
