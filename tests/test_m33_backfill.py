"""M3.3: enrich --retry-unavailable backfill + the survey origin_note upsert
regression (reviewer incident 2026-09-29: count(*) WHERE origin_note='survey'
returned 0 on the survey DB although draw_sample sets it)."""

import json

import pytest

from lastcopy.models import Edition, HitStatus, SourceHit, SourceName
from lastcopy.net import NormResponse
from lastcopy.store import Store
from lastcopy.survey import draw_sample


def _ed(i: int) -> Edition:
    from tests.test_survey import _isbn13

    return Edition(work_key=_isbn13(i), isbn13=_isbn13(i), title=f"Book {i}",
                   author=f"Author {i}", origin_note="survey")


# ----------------------------------------------------- origin_note regression
def test_m33_regression_survey_origin_note_survives_provenance_less_reupsert(tmp_path):
    """Named for the reviewer incident: a re-upsert of an Edition whose
    origin_note defaulted to '' wiped the stored 'survey' provenance, so
    SELECT count(*) ... WHERE origin_note='survey' returned 0."""
    store = Store(tmp_path / "r.db")
    ed = _ed(0)
    store.upsert_edition(ed)
    # provenance-less rebuild of the same row (e.g. a bib-data refresh path)
    store.upsert_edition(Edition(work_key=ed.work_key, isbn13=ed.isbn13,
                                 title=ed.title, author=ed.author))
    n = store.conn.execute(
        "SELECT count(*) FROM editions WHERE origin_note='survey'").fetchone()[0]
    assert n == 1  # was 0 pre-fix (upsert overwrote with '')
    store.close()


def test_upsert_edition_explicit_origin_note_still_wins(tmp_path):
    store = Store(tmp_path / "r.db")
    store.upsert_edition(_ed(0))
    store.upsert_edition(Edition(work_key=_ed(0).work_key, isbn13=_ed(0).isbn13,
                                 title="Relabeled", origin_note="csv"))
    row = store.conn.execute(
        "SELECT origin_note, title FROM editions WHERE work_key=?",
        (_ed(0).work_key,)).fetchone()
    assert row["origin_note"] == "csv" and row["title"] == "Relabeled"
    store.close()


def test_draw_sample_origin_note_persisted_end_to_end(tmp_path):
    import asyncio

    from tests.test_survey import _make_client, _ol_docs, _ol_router

    store = Store(tmp_path / "s.db")
    client = _make_client(store, _ol_router(_ol_docs(3, 1950)))
    asyncio.run(draw_sample(client, store, 3, 1950, 1960, sources=("ia",)))
    n = store.conn.execute(
        "SELECT count(*) FROM editions WHERE origin_note='survey'").fetchone()[0]
    assert n == 3
    store.close()


# ----------------------------------------------------- retry-unavailable
def _seed_unavailable(store: Store, eds: list[Edition],
                      unavailable: set[str]) -> None:
    for ed in eds:
        store.upsert_edition(ed)
        store.enqueue(ed.work_key, "gb")
        status = (HitStatus.unavailable if ed.work_key in unavailable
                  else HitStatus.ok)
        store.save_hit(SourceHit(work_key=ed.work_key, source=SourceName.gb,
                                 status=status))
        store.queue_mark(ed.work_key, "gb", "done")  # post-run state: all done


def test_retry_unavailable_reenqueues_only_unavailable_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "q.db")
    eds = [_ed(0), _ed(1), _ed(2)]
    unavail = {eds[0].work_key, eds[2].work_key}
    _seed_unavailable(store, eds, unavail)

    pairs = store.retry_unavailable()
    assert sorted(pairs) == sorted((wk, "gb") for wk in unavail)
    pending = {r["work_key"] for r in store.pending("gb")}
    assert pending == unavail  # done rows for ok hits stay done
    assert {r["work_key"] for r in store.conn.execute(
        "SELECT work_key FROM queue WHERE status='done'")} == {eds[1].work_key}

    pairs2 = store.retry_unavailable()  # idempotent: same set, no duplicates
    assert sorted(pairs2) == sorted(pairs)
    assert store.conn.execute("SELECT count(*) FROM queue").fetchone()[0] == 3
    store.close()


def test_retry_unavailable_evicts_cached_error_bodies(tmp_path):
    """429/quota bodies are cached with the 30d TTL; without eviction the
    backfill would replay them and stay unavailable forever."""
    from lastcopy.sources.gbooks import VOLUMES_URL

    store = Store(tmp_path / "q.db")
    ed = _ed(0)
    _seed_unavailable(store, [ed], {ed.work_key})
    params = {"q": f"isbn:{ed.isbn13}", "key": "k"}
    store.cache_put(VOLUMES_URL, params, 429, '{"error": "quotaExceeded"}')
    assert store.cache_get(VOLUMES_URL, params) is not None

    store.retry_unavailable()
    assert store.cache_get(VOLUMES_URL, params) is None
    store.close()


def test_enrich_retry_unavailable_flag_rechecks_gb(tmp_path, monkeypatch):
    """CLI e2e (mock transport): --retry-unavailable resets the done queue row,
    drops the cached 429, and the re-check records a fresh ok hit."""
    import asyncio
    from collections import namedtuple

    from lastcopy import cli as cli_mod
    from lastcopy.sources.gbooks import VOLUMES_URL

    monkeypatch.setenv("LASTCOPY_GBOOKS_KEY", "test-key-not-real")

    store = Store(tmp_path / "e.db")
    ed = _ed(0)
    _seed_unavailable(store, [ed], {ed.work_key})
    params = {"q": f"isbn:{ed.isbn13}", "key": "test-key-not-real"}
    store.cache_put(VOLUMES_URL, params, 429, '{"error": "quotaExceeded"}')
    store.close()

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None):
            self.calls.append((url, dict(params or {})))
            return NormResponse(status=200, body=json.dumps(
                {"totalItems": 0, "items": []}), ok=True)

        async def aclose(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(cli_mod, "PoliteClient", lambda store, **kw: fake)

    Args = namedtuple("Args", "db workers source retry_unavailable")
    rc = cli_mod.cmd_enrich(Args(db=str(tmp_path / "e.db"), workers=1,
                                 source="gb", retry_unavailable=True))
    assert rc == 0
    assert fake.calls and fake.calls[0][0] == VOLUMES_URL  # actually re-asked gb

    store = Store(tmp_path / "e.db")
    hit = store.conn.execute(
        "SELECT status FROM source_hits WHERE source='gb'").fetchone()
    assert hit["status"] == "ok"  # was unavailable; backfill re-decided it
    assert store.pending("gb") == []  # queue row done again
    store.close()


def test_enrich_without_flag_leaves_done_rows_alone():
    from lastcopy.cli import build_parser

    args = build_parser().parse_args(["enrich"])
    assert args.retry_unavailable is False
    args = build_parser().parse_args(["enrich", "--retry-unavailable"])
    assert args.retry_unavailable is True
