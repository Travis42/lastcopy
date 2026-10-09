"""M8.2 — lobid dump joiner tests (SPEC-M8.2-LOBID-JOIN).

Fixture: tests/fixtures/lobid_sample.jsonl holds REAL excerpts from the
lobid-resources 2026-10-04 dump (head-sampled 2026-10-09, arrays trimmed):
record 1 carries isbn ["8882660672", "9788882660673"] (isbn-10 + -13 pair)
and one hasItem heldBy {isil: "DE-5-13"}; record 2 (Odyssea) has no isbn
and a single DE-Kn28 item.  Join/import/CLI run against synthesized .gz
dumps in tmp_path — no network, no 22 GB dump.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from lastcopy import m4, m5, m8_lobid
from lastcopy.m8_lobid import main as join_main
from lastcopy.m8_lobid import main_import

FIXTURE = Path(__file__).parent / "fixtures" / "lobid_sample.jsonl"
SAMPLE = [json.loads(line) for line in
          FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def isbn13_for(core12: str) -> str:
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core12))
    return core12 + str((10 - total % 10) % 10)


WS_A = "9788882660673"                       # real fixture record 1 isbn13
WS_B = isbn13_for("978346805120")           # synthesized non-fixture match
WS_C = isbn13_for("978346805121")           # matched ONLY via isbn-10 form


def isbn10_for(isbn13: str) -> str:
    core9 = isbn13[3:12]
    total = sum(int(c) * (10 - i) for i, c in enumerate(core9))
    check = (11 - total % 11) % 11
    return core9 + ("X" if check == 10 else str(check))


# isbn-10 form of WS_C (978-prefix -> an equivalent isbn-10 exists)
ISBN10_C = isbn10_for(WS_C)


# ---------------------------------------------------------------- extract
def test_extract_real_fixture_record():
    isbns, libs = m8_lobid.extract(SAMPLE[0])
    assert isbns == {"9788882660673"}         # isbn10 folded into isbn13
    assert libs == {"DE-5-13"}                # isil short code


def test_extract_holdby_id_and_label_variants():
    base = {"isbn": ["9788882660673"]}
    r1 = {**base, "hasItem": [{"heldBy": {
        "id": "http://lobid.org/organisations/DE-Kn28#!",
        "label": "Erzbischöfliche Diözesan- und Dombibliothek"}}]}
    r2 = {**base, "hasItem": [{"heldBy": {"label": "Some Library, No Id"}}]}
    assert m8_lobid.extract(r1)[1] == {"DE-Kn28"}   # last path segment
    assert m8_lobid.extract(r2)[1] == {"Some Library, No Id"}
    # both items dedupe to one code when isil matches the id fallback
    r3 = {**base, "hasItem": [
        {"heldBy": {"isil": "DE-5-13", "id": "http://lobid.org/organisations/DE-5-13#!"}},
        {"heldBy": {"id": "http://lobid.org/organisations/DE-5-13#!"}}]}
    assert m8_lobid.extract(r3)[1] == {"DE-5-13"}


def test_extract_holdings_less_and_bad_isbn():
    assert m8_lobid.extract({"isbn": ["9788882660673"]}) is None   # no hasItem
    assert m8_lobid.extract({"hasItem": []}) is None
    assert m8_lobid.extract({"hasItem": [{"heldBy": None}]}) is None
    got = m8_lobid.extract({"isbn": ["notanisbn", "978-0-00-000000-0"],
                            "hasItem": [{"heldBy": {"isil": "DE-1"}}]})
    assert got is not None and got[0] == set()    # bad isbns skipped, held


def test_iter_records_bad_line_count(tmp_path):
    p = tmp_path / "mini.jsonl.gz"
    good = json.dumps(SAMPLE[0])
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(good + "\n")
        fh.write("{not json\n")
        fh.write("\n")
        fh.write(good + "\n")
        fh.write('["not", "a", "dict"]\n')
    stream = m8_lobid.iter_records(p)
    recs = list(stream)
    assert len(recs) == 2
    assert stream.bad_lines == 2


# ------------------------------------------------------------------- join
def _join_fixture(tmp_path: Path) -> Path:
    """10-record gz dump: 3 records match the workset (one only via
    isbn-10), 7 are non-matching filler (incl. one bib-only record)."""
    recs = []
    recs.append(SAMPLE[0])                                   # match A
    recs.append({"title": "Filler One", "isbn": [WS_B[:-1] + "0" if WS_B[-1] != "0" else WS_B[:-1] + "1"],
                 "hasItem": [{"heldBy": {"isil": "DE-2"}}]})  # bad checksum
    recs.append({"title": "Match B", "isbn": [WS_B],
                 "hasItem": [
                     {"heldBy": {"isil": "DE-294"}},
                     {"heldBy": {"isil": "DE-386"}}]})
    recs.append({"title": "Filler Two (bib only, no hasItem)",
                 "isbn": [isbn13_for("9781111111111")]})
    recs.append({"title": "Match C via isbn10", "isbn": [ISBN10_C],
                 "hasItem": [
                     {"heldBy": {"isil": "DE-61"}},
                     {"heldBy": {"isil": "DE-465"}},
                     {"heldBy": {"isil": "DE-290"}}]})
    recs.append({"title": "Filler Three", "isbn": [isbn13_for("9782222222222")],
                 "hasItem": [{"heldBy": {"isil": "DE-9"}}]})
    recs.append({"title": "Filler Four", "hasItem": [{"heldBy": {"isil": "DE-9"}}]})
    recs.append({"title": "Filler Five", "isbn": [isbn13_for("9783333333333")],
                 "hasItem": [{"heldBy": {"isil": "DE-82"}},
                                 {"heldBy": {"isil": "DE-82"}}]})   # dup lib
    recs.append(SAMPLE[1])                                   # no isbn → skip
    recs.append({"title": "Filler Six", "isbn": [isbn13_for("9784444444444")],
                 "hasItem": [{"heldBy": {"isil": "DE-70"}}]})
    p = tmp_path / "mini_dump.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def test_join_dump_rows_dedupe_nlibs(tmp_path):
    dump = _join_fixture(tmp_path)
    workset = {WS_A, WS_B, WS_C}
    out = tmp_path / "lobid_matches.db"
    stats = m8_lobid.join_dump(dump, workset, out, progress_every=3)
    assert stats["lines"] == 10
    assert stats["matched_records"] == 3
    assert stats["unique_isbns"] == 3
    assert stats["n_libs_distribution"] == {"1": 1, "2": 1, "3+": 1}
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM lobid_matches "
                        "ORDER BY isbn13").fetchall()
    assert len(rows) == 3                     # isbn10 folded -> no dup row
    by_isbn = {r["isbn13"]: r for r in rows}
    a = by_isbn[WS_A]
    assert a["libraries"] == "DE-5-13" and a["n_libs"] == 1
    assert a["title"] == SAMPLE[0]["title"]
    assert a["lobid_id"] == SAMPLE[0]["hbzId"]
    assert by_isbn[WS_B]["n_libs"] == 2
    assert by_isbn[WS_C]["n_libs"] == 3
    assert by_isbn[WS_C]["libraries"] == "DE-290 DE-465 DE-61"
    conn.close()


def test_lobid_join_cli_smoke(tmp_path, capsys):
    dump = _join_fixture(tmp_path)
    ws = tmp_path / "ws.isbns"
    ws.write_text("# comment\n" + f"{WS_A}\n{WS_B}\n{WS_C}\n\n",
                  encoding="utf-8")
    out = tmp_path / "out.db"
    assert join_main(["--dump", str(dump), "--workset", str(ws),
                      "--out", str(out)]) == 0
    assert capsys.readouterr().err.count("lines=") >= 1
    n = sqlite3.connect(out).execute(
        "SELECT COUNT(*) FROM lobid_matches").fetchone()[0]
    assert n == 3


# ------------------------------------------------------------ lab import
def test_import_matches_into_lab_db(tmp_path):
    dump = _join_fixture(tmp_path)
    matches = tmp_path / "lobid_matches.db"
    m8_lobid.join_dump(dump, {WS_A, WS_B, WS_C}, matches)

    lab = m4.connect(tmp_path / "m4.db")
    m5.ensure_schema(lab)
    lab.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (WS_A,))
    lab.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (WS_B,))
    lab.commit()
    stats = m8_lobid.import_matches(matches, lab)
    assert stats["inserted"] == 3
    rows = lab.execute("SELECT isbn13, institution, record_id, detail "
                       "FROM holdings WHERE institution='lobid' "
                       "ORDER BY isbn13").fetchall()
    assert len(rows) == 3
    by_isbn = {r["isbn13"]: r for r in rows}
    assert by_isbn[WS_A]["detail"] == "DE-5-13"
    assert by_isbn[WS_A]["record_id"] == SAMPLE[0]["hbzId"]
    # custody re-derivation ran alongside (WS_A single-institution -> single)
    cust = dict(lab.execute("SELECT isbn13, custody_physical FROM "
                            "enrich_status").fetchall())
    assert cust[WS_A] == "single"
    # idempotent: rerun inserts nothing
    assert m8_lobid.import_matches(matches, lab)["inserted"] == 0
    lab.close()


def test_lobid_import_cli_smoke(tmp_path):
    dump = _join_fixture(tmp_path)
    matches = tmp_path / "lobid_matches.db"
    m8_lobid.join_dump(dump, {WS_A}, matches)
    lab_path = tmp_path / "m4.db"
    lab = m4.connect(lab_path)
    m5.ensure_schema(lab)
    lab.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (WS_A,))
    lab.commit()
    lab.close()
    rc = main_import(["--matches", str(matches), "--db", str(lab_path)])
    assert rc == 0
    lab = m4.connect(lab_path)
    assert lab.execute("SELECT COUNT(*) FROM holdings WHERE "
                       "institution='lobid'").fetchone()[0] == 1
    lab.close()
