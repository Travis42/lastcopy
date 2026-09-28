"""E2E: ingest -> enrich(mock transport over real fixtures) -> classify -> report (golden)."""

import csv
import io
import json
from pathlib import Path

import pytest

from lastcopy import cli
from tests.conftest import Router, load

GREEN_ISBN = "9780486417783"   # Aesop's Fables (Dover) — WD hit -> PG full text
MODERN_ISBN = "9780060935467"  # To Kill a Mockingbird — all misses -> RED-UNVERIFIED

LOT_CSV = f"""isbn,title,author,year,publisher
{GREEN_ISBN},Aesop's Fables,Aesop,1998,Dover
{MODERN_ISBN},To Kill a Mockingbird,Harper Lee,1988,Warner Books
,Tirbol de Maio,Antero de Quental,1863,  -- pre-ISBN bib stub row
"""


def test_key_required_stubs_raise_clear_errors():
    import asyncio
    from lastcopy.sources.gbooks import KeyRequiredError, check as gb_check
    from lastcopy.sources import hathi
    with pytest.raises(KeyRequiredError, match="M3"):
        asyncio.run(gb_check())
    with pytest.raises(KeyRequiredError, match="M3"):
        asyncio.run(hathi.check())


def test_cli_enrich_rejects_gated_source(capsys):
    assert cli.main(["enrich", "--source", "gb"]) == 2
    assert "requires a key" in capsys.readouterr().err


def run_cli(*argv) -> str:
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    assert rc == 0
    return buf.getvalue()


def test_e2e_golden_report(tmp_path, monkeypatch):
    db = tmp_path / "e2e.db"
    lot = tmp_path / "lot.csv"
    lot.write_text(LOT_CSV, encoding="utf-8")

    # --- ingest
    out = run_cli("--db", str(db), "ingest", "--csv", str(lot))
    assert "3 rows" in out and "bib-stub" in out

    # --- enrich over recorded real payloads (mock transport)
    router = (Router()
              # OL: green misses search (deterministic), modern misses too
              .add_q_contains(GREEN_ISBN, "ol_search_miss", host="openlibrary.org")
              .add_q_contains(MODERN_ISBN, "ol_search_miss", host="openlibrary.org")
              .add_default("ol_search_miss", host="openlibrary.org"))
    # IA: green has an IA hit; modern misses
    router.add_q_contains(GREEN_ISBN, "ia_hit", host="archive.org")
    router.add_default("ia_miss", host="archive.org")
    # WD: green has the PG hit (isbn13 exact match variant)
    router.add(lambda req, p: req.url.host == "query.wikidata.org" and
               (GREEN_ISBN in json.dumps(p) or GREEN_ISBN in str(req.url)),
               (200, load("wd_hit")))
    router.add_default("wd_miss", host="query.wikidata.org")

    import httpx
    import lastcopy.cli as cli_mod
    real_client_cls = cli_mod.PoliteClient

    class FixtureClient(real_client_cls):
        def __init__(self, store, **kw):
            super().__init__(store, transport=httpx.MockTransport(router.handler),
                             min_interval=0.0, jitter_max=0.0)

    monkeypatch.setattr(cli_mod, "PoliteClient", FixtureClient)
    run_cli("--db", str(db), "enrich")

    # --- classify
    out = run_cli("--db", str(db), "classify")
    assert "GREEN=1" in out and "RED-UNVERIFIED=1" in out and "UNKNOWN=1" in out

    # --- report
    md_path = tmp_path / "report.md"
    csv_path = tmp_path / "report.csv"
    md = run_cli("--db", str(db), "report", "--md", str(md_path), "--csv", str(csv_path))
    assert md_path.exists() and csv_path.exists()

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    by_key = {r["work_key"]: r for r in rows}
    assert by_key[GREEN_ISBN]["class"] == "GREEN"
    assert "gutenberg.org/ebooks/215" in by_key[GREEN_ISBN]["evidence_urls"]
    assert by_key[MODERN_ISBN]["class"] == "RED-UNVERIFIED"
    stub = [r for r in rows if r["work_key"].startswith("bib-")]
    assert stub and stub[0]["class"] == "UNKNOWN"

    text = md_path.read_text(encoding="utf-8")
    assert "RED list" in text
    assert "Roll-up per work" in text
    assert "Counts by class" in text
    # RED list comes first and modern bestseller is in it with an explicit rule
    red_pos = text.index("RED list")
    modern_pos = text.index("To Kill a Mockingbird")
    assert red_pos < modern_pos
    assert "no-surrogate+holdings-unknown" in text
    # every GREEN entry row carries surrogate evidence URLs
    assert "archive.org/details" in text


def test_ingest_invalid_isbn_falls_to_bib_stub(tmp_path):
    db = tmp_path / "ing.db"
    lot = tmp_path / "bad.csv"
    lot.write_text("isbn,title,author,year\n9780140328722,Bad Checksum,Someone,1999\n",
                   encoding="utf-8")
    out = run_cli("--db", str(db), "ingest", "--csv", str(lot))
    assert "invalid-ISBN fallbacks" in out
    from lastcopy.store import Store
    s = Store(db)
    eds = s.all_editions()
    assert len(eds) == 1 and eds[0].work_key.startswith("bib-")
    assert eds[0].origin_note == "bib-stub:invalid-isbn"
    assert [r["source"] for r in s.pending("bib-stub")] == ["bib-stub"]
    s.close()


def test_bib_mode_keys_all_rows(tmp_path):
    db = tmp_path / "bib.db"
    lot = tmp_path / "bib.csv"
    lot.write_text("isbn,title,author,year\n9780140328721,Fantastic Mr Fox,Roald Dahl,1970\n"
                   ",Sem Titulo,Autor,1910\n", encoding="utf-8")
    out = run_cli("--db", str(db), "ingest", "--csv", str(lot), "--bib-mode")
    assert "2 bib-stub" in out
    from lastcopy.store import Store
    s = Store(db)
    assert all(e.work_key.startswith("bib-") for e in s.all_editions())
    s.close()
