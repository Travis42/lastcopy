"""M2/M3.2 survey: Wilson CI math, draw with rarity fields, slices, unknown split,
rows-CSV golden, thin-sample flag, migration. Mock transport throughout."""

import csv
import json

import httpx
import pytest

from lastcopy import survey
from lastcopy.models import Edition, HitStatus, SourceHit, SourceName, Surrogate, SurrogateAccess
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


# ------------------------------------------------------------------ draw_sample
def _make_client(store, router):
    return PoliteClient(store, transport=httpx.MockTransport(router.handler),
                        min_interval=0.0, jitter_max=0.0)


def _isbn13(i: int) -> str:
    core = f"978{i:09d}"
    total = sum(int(c) * (1 if j % 2 == 0 else 3) for j, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def _ol_docs(n: int, year: int, idx_offset: int = 0,
             edition_counts=None, languages=None) -> list[dict]:
    # shape-faithful to OL search.json: docs[] with isbn[], title,
    # first_publish_year, edition_count, language[]
    docs = []
    for i in range(idx_offset, idx_offset + n):
        d = {"isbn": [_isbn13(i)], "title": f"Book {i}",
             "author_name": [f"Author {i}"], "first_publish_year": year}
        if edition_counts is not None:
            ec = edition_counts[i - idx_offset]
            if ec is not None:
                d["edition_count"] = ec  # missing key = OL omitted the field
        if languages is not None:
            lang = languages[i - idx_offset]
            if lang is not None:
                d["language"] = [lang]
        docs.append(d)
    return docs


def _ol_router(docs: list[dict], **kwargs) -> Router:
    router = Router()
    router.add(lambda req, p: req.url.host == "openlibrary.org",
               (200, json.dumps({"numFound": len(docs), "docs": docs})))
    return router


def test_draw_sample_persists_edition_count_and_language(tmp_path):
    import asyncio

    store = Store(tmp_path / "s.db")
    docs = _ol_docs(4, 1920, edition_counts=[1, None, 7, 2],
                    languages=["eng", None, "por", "fre"])
    client = _make_client(store, _ol_router(docs))
    editions = asyncio.run(survey.draw_sample(client, store, 4, 1900, 1930,
                                              sources=("ia",)))
    assert [e.edition_count for e in editions] == [1, None, 7, 2]
    assert [e.language for e in editions] == ["eng", None, "por", "fre"]
    row = store.conn.execute(
        "SELECT edition_count, language FROM editions WHERE work_key=?",
        (editions[0].work_key,)).fetchone()
    assert row["edition_count"] == 1 and row["language"] == "eng"
    row_missing = store.conn.execute(
        "SELECT edition_count, language FROM editions WHERE work_key=?",
        (editions[1].work_key,)).fetchone()
    assert row_missing["edition_count"] is None and row_missing["language"] is None
    store.close()


def test_draw_sample_filter_appended_to_query(tmp_path):
    import asyncio

    store = Store(tmp_path / "s.db")
    router = _ol_router(_ol_docs(2, 1950))
    client = _make_client(store, router)
    asyncio.run(survey.draw_sample(client, store, 2, 1950, 1960,
                                   sources=("ia",), query_filter="language:por"))
    ol_reqs = [r for r in router.requests if r.url.host == "openlibrary.org"]
    assert ol_reqs and "language:por" in str(ol_reqs[0].url.params["q"])
    assert "publish_year:[1950 TO 1960]" in str(ol_reqs[0].url.params["q"])
    store.close()


def test_draw_sample_enqueues_only_given_sources(tmp_path):
    import asyncio

    store = Store(tmp_path / "s.db")
    client = _make_client(store, _ol_router(_ol_docs(2, 1950)))
    asyncio.run(survey.draw_sample(client, store, 2, 1950, 1960, sources=("ia", "gb")))
    queued = {r["source"] for r in store.conn.execute("SELECT source FROM queue")}
    assert queued == {"ia", "gb"}
    store.close()


# ------------------------------------------------------------------ summarize
def _edition(i: int, year=1950, ec=None, language=None) -> Edition:
    return Edition(work_key=_isbn13(i), isbn13=_isbn13(i), title=f"Book {i}",
                   author=f"Author {i}", year=year, edition_count=ec,
                   language=language, origin_note="survey")


def _seed(store: Store, editions: list[Edition], green: set[str] = set(),
          unavailable: set[str] = set()):
    for ed in editions:
        store.upsert_edition(ed)
        if ed.work_key in unavailable:
            store.save_hit(SourceHit(work_key=ed.work_key, source=SourceName.ia,
                                     status=HitStatus.unavailable))
        else:
            store.save_hit(SourceHit(work_key=ed.work_key, source=SourceName.ia,
                                     status=HitStatus.ok))
            if ed.work_key in green:
                store.save_surrogates(ed.work_key, [Surrogate(
                    work_key=ed.work_key, provider="ia",
                    access=SurrogateAccess.public, identifier=ed.work_key,
                    url=f"https://archive.org/details/{ed.work_key}")])


def test_slice_bucket_boundaries(tmp_path):
    store = Store(tmp_path / "s.db")
    eds = [_edition(0, ec=1), _edition(1, ec=2), _edition(2, ec=3),
           _edition(3, ec=4), _edition(4, ec=99), _edition(5, ec=None)]
    _seed(store, eds)
    r = survey.summarize(store, eds)
    s = r["slices"]
    assert s["editions:1"]["n"] == 1
    assert s["editions:2-3"]["n"] == 2
    assert s["editions:4+"]["n"] == 2  # 4 and 99
    assert s["all"]["n"] == 6
    store.close()


def test_era_bucket_boundaries(tmp_path):
    store = Store(tmp_path / "s.db")
    eds = [_edition(0, year=1926), _edition(1, year=1927), _edition(2, year=1969),
           _edition(3, year=1970), _edition(4, year=1999), _edition(5, year=None)]
    _seed(store, eds)
    s = survey.summarize(store, eds)["slices"]
    assert s["era:pre-1927"]["n"] == 1          # 1926 only
    assert s["era:1927-1969"]["n"] == 2         # 1927 and 1969
    assert s["era:1970+"]["n"] == 2             # 1970 and 1999
    store.close()


def test_language_slices_eng_vs_non_eng(tmp_path):
    store = Store(tmp_path / "s.db")
    eds = [_edition(0, language="eng"), _edition(1, language="por"),
           _edition(2, language="fre"), _edition(3, language=None)]
    _seed(store, eds)
    s = survey.summarize(store, eds)["slices"]
    assert s["lang:eng"]["n"] == 1
    assert s["lang:non-eng"]["n"] == 2
    store.close()


def test_unknown_split_headline_over_decided_only(tmp_path):
    store = Store(tmp_path / "s.db")
    eds = [_edition(0), _edition(1), _edition(2)]
    _seed(store, eds, green={eds[0].work_key}, unavailable={eds[2].work_key})
    r = survey.summarize(store, eds)
    assert r["sample"] == 3 and r["unknown"] == 1 and r["decided"] == 2
    assert r["slices"]["all"]["n"] == 2
    assert r["slices"]["all"]["no_surrogate"] == 1  # GREEN excluded, UNKNOWN excluded
    assert r["slices"]["all"]["pct"] == 50.0
    text = survey.render(r)
    assert "unknown=1" in text and "decided=2" in text
    store.close()


def test_thin_sample_flag(tmp_path):
    store = Store(tmp_path / "s.db")
    thin = [_edition(i, year=1950) for i in range(5)]
    _seed(store, thin)
    r_thin = survey.summarize(store, thin)
    assert r_thin["slices"]["all"]["thin"] is True
    assert "thin-sample" in survey.render(r_thin)

    fat = thin + [_edition(100 + i, year=1950) for i in range(25)]
    _seed(store, fat)
    r_fat = survey.summarize(store, fat)
    assert r_fat["slices"]["all"]["n"] == 30
    assert r_fat["slices"]["all"]["thin"] is False
    store.close()


# ------------------------------------------------------------------ rows CSV
def test_rows_csv_golden(tmp_path):
    store = Store(tmp_path / "s.db")
    ed_green = _edition(0, year=1920, ec=1, language="eng")
    ed_red = _edition(1, year=1975, ec=4, language="por")
    ed_unknown = _edition(2, year=1930, ec=2, language="ger")
    _seed(store, [ed_green, ed_red, ed_unknown],
          green={ed_green.work_key}, unavailable={ed_unknown.work_key})
    out = tmp_path / "rows.csv"
    survey.write_rows(out, store, [ed_green, ed_red, ed_unknown])
    rows = list(csv.reader(out.read_text().splitlines()))
    assert rows[0] == survey.ROWS_HEADER
    assert rows[0] == ["work_key", "isbn13", "title", "author", "year",
                       "edition_count", "language", "sources_checked",
                       "surrogates", "class", "rule"]
    by_key = {r[0]: r for r in rows[1:]}
    g = by_key[ed_green.work_key]
    assert g[5] == "1" and g[6] == "eng" and g[7] == "ia"
    assert g[8] == "ia:public" and g[9] == "GREEN" and g[10] == "public-surrogate"
    red = by_key[ed_red.work_key]
    assert red[8] == "" and red[9] == "RED-UNVERIFIED"
    assert red[10] == "no-surrogate+holdings-unknown"
    unk = by_key[ed_unknown.work_key]
    assert unk[9] == "UNKNOWN" and unk[10] == "source-unavailable"
    store.close()


# ------------------------------------------------------------------ migration
def test_migration_adds_nullable_columns_on_old_db(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE editions (
        work_key TEXT PRIMARY KEY, isbn13 TEXT, isbn10 TEXT, title TEXT,
        author TEXT, year INTEGER, publisher TEXT, imprint_place TEXT,
        language TEXT, origin_note TEXT);
    """)
    conn.execute(
        "INSERT INTO editions (work_key, origin_note) VALUES ('978x', 'csv')")
    conn.commit()
    conn.close()

    store = Store(db)  # pragma-guarded ALTER TABLE lands here
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(editions)")}
    assert "edition_count" in cols
    row = store.conn.execute(
        "SELECT edition_count FROM editions WHERE work_key='978x'").fetchone()
    assert row["edition_count"] is None  # nullable, existing rows untouched
    store.upsert_edition(Edition(work_key=_isbn13(9), isbn13=_isbn13(9),
                                 edition_count=3))
    assert store.all_editions()[0].edition_count == 3 or any(
        e.edition_count == 3 for e in store.all_editions())
    store.close()


# ------------------------------------------------------------------ e2e (mock)
def test_survey_pipeline_e2e(tmp_path, monkeypatch):
    import asyncio
    from lastcopy import cli as cli_mod

    monkeypatch.setenv("LASTCOPY_GBOOKS_KEY", "test-key-not-real")
    store = Store(tmp_path / "e.db")
    docs = _ol_docs(4, 1920, edition_counts=[1, 2, 4, 4],
                    languages=["eng", "por", "eng", "eng"])
    router = _ol_router(docs)
    router.add_default("ia_miss", host="archive.org")
    router.add_default("gb_miss", host="www.googleapis.com")
    # one edition gets an IA public scan -> not "no surrogate"
    ia_green_isbn = docs[0]["isbn"][0]
    router.routes.insert(0, (
        lambda req, p: req.url.host == "archive.org"
        and ia_green_isbn in str(p.get("q", "")),
        (200, load("ia_hit"))))
    client = _make_client(store, router)

    editions = asyncio.run(survey.draw_sample(client, store, 4, 1900, 1930,
                                              sources=("ia", "gb")))
    assert len(editions) == 4
    totals = asyncio.run(_enrich(store, client, cli_mod, ("ia", "gb")))
    assert totals["ok"] == 8  # 4 editions x (ia + gb), all miss-or-hit -> ok
    out = tmp_path / "rows.csv"
    survey.write_rows(out, store, editions)
    r = survey.summarize(store, editions)
    assert r["sample"] == 4 and r["unknown"] == 0
    assert r["slices"]["all"]["n"] == 4
    assert r["slices"]["all"]["no_surrogate"] == 3
    assert r["slices"]["editions:1"]["no_surrogate"] == 0  # the GREEN one
    assert r["slices"]["lang:non-eng"]["n"] == 1
    assert r["slices"]["era:pre-1927"]["n"] == 4
    text = survey.render(r)
    assert "Wilson 95% CI" in text and "thin-sample" in text
    assert out.read_text().splitlines()[0] == ",".join(survey.ROWS_HEADER)
    store.close()


async def _enrich(store, client, cli_mod, sources):
    from collections import Counter

    totals = Counter()
    editions = {e.work_key: e for e in store.all_editions()}
    for src in sources:
        check = cli_mod.SOURCE_CHECKS[src]
        for row in store.pending(src):
            ed = editions[row["work_key"]]
            out = await check(ed, client, store)
            hit, surrogates = out if isinstance(out, tuple) else (out, [])
            store.save_hit(hit)
            if surrogates:
                store.save_surrogates(ed.work_key, surrogates)
            store.queue_mark(ed.work_key, src, "done")
            totals[hit.status.value] += 1
    return totals
