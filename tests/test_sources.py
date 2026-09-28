"""Source normalizers against recorded real payloads, stubbed at the transport boundary."""

import asyncio
import json

from lastcopy.models import Edition, HitStatus, SurrogateAccess
from lastcopy.sources import ia, ol, wikidata
from tests.conftest import Router, load

GREEN_ED = Edition(work_key="9780140328721", isbn13="9780140328721", isbn10="0140328726",
                   title="Fantastic Mr Fox", origin_note="csv")
MODERN_ED = Edition(work_key="9780060935467", isbn13="9780060935467",
                    title="To Kill a Mockingbird", origin_note="csv")
BIB_ED = Edition(work_key="bib-x", title="T", author="A", origin_note="bib-stub:no-isbn")
PG_ED = Edition(work_key="9780486417783", isbn13="9780486417783", origin_note="csv")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ Open Library
def test_ol_hit(make_client, store):
    r = (Router()
         .add_q_contains("9780140328721", "ol_search_hit")
         .add_url_contains("editions.json", "ol_editions_hit")
         .add_default("ol_search_miss"))
    client = make_client(r)
    hit = run(ol.check(GREEN_ED, client, store))
    assert hit.status is HitStatus.ok
    ev = hit.evidence_json
    assert ev["ol_work_key"] == "/works/OL45804W"
    assert ev["title"] == "Fantastic Mr Fox"
    assert isinstance(ev["oclc_numbers"], list)
    assert ev["url"].startswith("https://openlibrary.org")
    r.assert_polite_ua()


def test_ol_miss_is_ok_zero(make_client, store):
    hit = run(ol.check(MODERN_ED, make_client(Router().add_default("ol_search_miss")), store))
    assert hit.status is HitStatus.ok
    assert hit.evidence_json["numFound"] == 0


def test_ol_error_body_is_unavailable(make_client, store):
    # real 404 HTML body from openlibrary.org
    hit = run(ol.check(GREEN_ED, make_client(Router().add_default("ol_error")), store))
    assert hit.status is HitStatus.unavailable


def test_ol_429_is_unavailable_never_crash(make_client, store):
    r = Router().add_default("ol_search_miss")
    r.routes = [(r.routes[0][0], (429, '{"error": "Quota exceeded"}'))]
    hit = run(ol.check(GREEN_ED, make_client(r), store))
    assert hit.status is HitStatus.unavailable


def test_ol_empty_body_is_unavailable(make_client, store):
    r = Router().add_default("ol_search_miss")
    r.routes = [(r.routes[0][0], (200, ""))]
    hit = run(ol.check(GREEN_ED, make_client(r), store))
    assert hit.status is HitStatus.unavailable


def test_ol_bib_stub_is_skipped(make_client, store):
    hit = run(ol.check(BIB_ED, make_client(Router()), store))
    assert hit.status is HitStatus.skipped


# ------------------------------------------------------------------ Internet Archive
def test_ia_hit_yields_public_surrogates(make_client, store):
    r = (Router()
         .add_q_contains("9780140328721", "ia_hit")
         .add_default("ia_miss"))
    hit, surrogates = run(ia.check(GREEN_ED, make_client(r), store))
    assert hit.status is HitStatus.ok
    assert len(surrogates) > 0
    assert all(s.url.startswith("https://archive.org/details/") for s in surrogates)
    assert any(s.access is SurrogateAccess.public for s in surrogates)
    r.assert_polite_ua()


def test_ia_error_body_is_unavailable(make_client, store):
    # IA returns HTTP 200 with {"error": ...}; must normalize to unavailable
    hit, surrogates = run(ia.check(GREEN_ED, make_client(Router().add_default("ia_error")), store))
    assert hit.status is HitStatus.unavailable
    assert surrogates == []


def test_ia_miss_is_ok_zero(make_client, store):
    hit, surrogates = run(ia.check(MODERN_ED, make_client(Router().add_default("ia_miss")), store))
    assert hit.status is HitStatus.ok
    assert surrogates == []
    assert hit.evidence_json["num_hits"] == 0


def test_ia_inlibrary_collection_is_lending(make_client, store):
    # shape-faithful mutation of the real ia_hit payload: collection -> inlibrary
    body = json.loads(load("ia_hit"))
    docs = body["response"]["docs"]
    assert docs
    docs[0]["collection"] = ["inlibrary", "printdisabled"]
    payload = json.dumps(body)
    r = (Router()
         .add_q_contains("9780140328721", "ia_hit")
         .add_default("ia_miss"))
    r.routes[0] = (lambda req, p: "9780140328721" in str(p.get("q", "")), (200, payload))
    hit, surrogates = run(ia.check(GREEN_ED, make_client(r), store))
    assert hit.status is HitStatus.ok
    first = next(s for s in surrogates if s.identifier == docs[0]["identifier"])
    assert first.access is SurrogateAccess.lending


def test_ia_oclc_fallback_uses_ol_evidence(make_client, store):
    # IA second query uses oclc: from stored OL evidence (HT-pass-two prep, SPEC edge)
    from lastcopy.models import SourceHit, SourceName
    store.save_hit(SourceHit(work_key=GREEN_ED.work_key, source=SourceName.ol,
                             status=HitStatus.ok,
                             evidence_json={"oclc_numbers": ["12345"]}))
    r = (Router()
         .add_q_contains("9780140328721", "ia_miss")
         .add_q_contains("oclc:12345", "ia_miss")
         .add_default("ia_miss"))
    hit, _ = run(ia.check(GREEN_ED, make_client(r), store))
    assert hit.status is HitStatus.ok
    assert any("oclc%3A12345" in str(req.url) or "oclc:12345" in str(req.url)
               for req in r.requests)


# ------------------------------------------------------------------ Wikidata
def test_wd_hit_yields_gutenberg_surrogate(make_client, store):
    r = Router().add_default("wd_hit")
    hit, surrogates = run(wikidata.check(PG_ED, make_client(r), store))
    assert hit.status is HitStatus.ok
    assert any(s.provider == "wikidata:projectgutenberg" and s.access is SurrogateAccess.public
               for s in surrogates)
    assert any("gutenberg.org/ebooks/215" in s.url for s in surrogates)
    r.assert_polite_ua()


def test_wd_miss_is_ok_zero(make_client, store):
    hit, surrogates = run(wikidata.check(MODERN_ED, make_client(Router().add_default("wd_miss")), store))
    assert hit.status is HitStatus.ok
    assert surrogates == []


def test_wd_sparql_400_is_unavailable(make_client, store):
    hit, surrogates = run(wikidata.check(PG_ED, make_client(Router().add_default("wd_error", status=400)), store))
    assert hit.status is HitStatus.unavailable


def test_wd_429_is_unavailable(make_client, store):
    r = Router().add_default("wd_miss")
    r.routes = [(r.routes[0][0], (429, ""))]
    hit, surrogates = run(wikidata.check(PG_ED, make_client(r), store))
    assert hit.status is HitStatus.unavailable
