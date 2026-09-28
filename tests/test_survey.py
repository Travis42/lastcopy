"""M2 survey: Wilson CI math + survey pipeline over fixture transport."""

import json
import math

import httpx
import pytest

from lastcopy import survey
from lastcopy.net import PoliteClient
from lastcopy.store import Store
from tests.conftest import Router, load


def test_wilson_ci_known_values():
    lo, hi = survey.wilson_ci(50, 100)
    assert abs(lo - 0.4038) < 0.001 and abs(hi - 0.5962) < 0.001
    lo, hi = survey.wilson_ci(0, 100)
    assert lo == 0.0 and abs(hi - 0.0370) < 0.001
    lo, hi = survey.wilson_ci(100, 100)
    assert abs(lo - 0.9630) < 0.001 and round(hi, 9) == 1.0
    assert survey.wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_contains_point_estimate():
    for k, n in ((3, 10), (7, 30), (200, 500)):
        lo, hi = survey.wilson_ci(k, n)
        assert lo <= k / n <= hi


def _make_client(store, router):
    return PoliteClient(store, transport=httpx.MockTransport(router.handler),
                        min_interval=0.0, jitter_max=0.0)


def _ol_sample_body(n_docs: int, year: int, idx_offset: int = 0) -> str:
    # shape-faithful to OL search.json: docs[] with isbn[], title, first_publish_year
    docs = []
    for i in range(idx_offset, idx_offset + n_docs):
        core = f"978{i:09d}"
        total = sum(int(c) * (1 if j % 2 == 0 else 3) for j, c in enumerate(core))
        isbn = core + str((10 - total % 10) % 10)
        docs.append({"isbn": [isbn], "title": f"Book {i}",
                     "author_name": [f"Author {i}"], "first_publish_year": year})
    return json.dumps({"numFound": len(docs), "docs": docs})


def test_survey_pipeline_all_no_surrogate(tmp_path):
    import asyncio
    from lastcopy import cli as cli_mod
    store = Store(tmp_path / "s.db")
    router = Router().add_default("ia_miss", host="archive.org")
    router.add_default("wd_miss", host="query.wikidata.org")
    client = _make_client(store, router)
    # OL sample fixture route: shape-faithful synthetic search.json
    sample_body = _ol_sample_body(8, 1920)
    router.add(lambda req, p: req.url.host == "openlibrary.org", (200, sample_body))

    editions = asyncio.run(survey.draw_sample(client, store, 8, 1900, 1930))
    assert len(editions) == 8 and all(e.work_key.startswith("978") for e in editions)
    for src in ("ia", "wd"):
        check = cli_mod.KEYLESS_SOURCES[src]
        for row in store.pending(src):
            ed = next(e for e in editions if e.work_key == row["work_key"])
            out = asyncio.run(check(ed, client, store))
            hit, surrogates = out if isinstance(out, tuple) else (out, [])
            store.save_hit(hit)
            store.queue_mark(ed.work_key, src, "done")
    report = survey.summarize(store, editions)
    assert report["sample"] == 8
    assert report["all"]["no_surrogate"] == 8
    assert report["all"]["pct"] == 100.0
    assert report["pre-1927"]["n"] == 8  # all sampled docs publish year 1920
    text = survey.render(report)
    assert "Wilson 95% CI" in text and "pre-1927" in text and "post-1927" in text
    store.close()


def test_survey_split_pre_post_1927(tmp_path):
    import asyncio
    from lastcopy import cli as cli_mod
    store = Store(tmp_path / "s2.db")
    pre = json.loads(_ol_sample_body(4, 1900))
    post = json.loads(_ol_sample_body(4, 1950, idx_offset=100))
    docs = pre["docs"] + post["docs"]
    body = json.dumps({"numFound": 8, "docs": docs})
    # one post-1927 book gets an IA public scan -> not "no surrogate"
    router = Router().add_default("ia_miss", host="archive.org")
    router.add_default("wd_miss", host="query.wikidata.org")
    router.add(lambda req, p: req.url.host == "openlibrary.org", (200, body))
    ia_green_isbn = post["docs"][0]["isbn"][0]
    ia_hit = load("ia_hit")
    router.routes.insert(0, (
        lambda req, p: req.url.host == "archive.org" and ia_green_isbn in str(p.get("q", "")),
        (200, ia_hit)))
    client = _make_client(store, router)

    editions = asyncio.run(survey.draw_sample(client, store, 8, 1900, 1960))
    for src in ("ia", "wd"):
        check = cli_mod.KEYLESS_SOURCES[src]
        for row in store.pending(src):
            ed = next(e for e in editions if e.work_key == row["work_key"])
            out = asyncio.run(check(ed, client, store))
            hit, surrogates = out if isinstance(out, tuple) else (out, [])
            store.save_hit(hit)
            if surrogates:
                store.save_surrogates(ed.work_key, surrogates)
            store.queue_mark(ed.work_key, src, "done")
    report = survey.summarize(store, editions)
    assert report["all"]["n"] == 8 and report["all"]["no_surrogate"] == 7
    assert report["pre-1927"]["n"] == 4 and report["pre-1927"]["no_surrogate"] == 4
    assert report["post-1927"]["n"] == 4 and report["post-1927"]["no_surrogate"] == 3
    store.close()
