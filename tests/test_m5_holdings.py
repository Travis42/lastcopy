"""M5.7 — national-library holdings tests (SPEC-M5.7-HOLDINGS).

Covers: hand-built MARC21-xml fixture (020 $a with ISBN-10 + hyphenated
ISBN-13, qualifier suffix, unmatched records) through parse-holdings-bulk
(plain + .gz + .zip paths, idempotency), shape-faithful SRU stubs per
institution (SRU namespaces, numberOfRecords, embedded MARC/MODS record
ids) through enrich-holdings (budget cap, resume-skip, http-for-loc,
2 rps pacing), assign_holdings_summary joined codes, and the holdings
export column.  No network access.
"""

from __future__ import annotations

import gzip
import json
import zipfile

import pytest

from lastcopy import m4, m5
from lastcopy.cli import main as cli_main
from lastcopy.isbn import isbn13_to_10

MARC_NS = "http://www.loc.gov/MARC21/slim"
SRW_NS = "http://www.loc.gov/zing/srw/"
MODS_NS = "http://www.loc.gov/mods/v3"
DC_NS = "http://purl.org/dc/elements/1.1/"


def isbn13_for(n: int, prefix: str = "9780") -> str:
    core = prefix + f"{n:08d}"
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


# ------------------------------------------------------------ test fixture db
@pytest.fixture()
def holdings_db(tmp_path):
    conn = m4.connect(tmp_path / "holdings.db")
    m5.ensure_schema(conn)
    for i in range(1, 13):
        conn.execute(
            "INSERT INTO enrich_workset (isbn13, work_key, edition_count, "
            "score, oclc, created_at) VALUES (?,?,1,?,NULL,?)",
            (isbn13_for(i), f"/works/H{i}", 10 - i, m5.now()))
        conn.execute("INSERT INTO enrich_status (isbn13) VALUES (?)",
                     (isbn13_for(i),))
    conn.commit()
    yield conn, tmp_path
    conn.close()


# ------------------------------------------------------------ MARC21 fixture
def marc_record(rec_id: str, isbns: list[str]) -> str:
    """One MARC21-slim <record>: 001 controlfield + one 020 datafield per
    raw ISBN string (qualifiers included where callers add them)."""
    fields = [f'<marc:controlfield tag="001">{rec_id}</marc:controlfield>']
    for raw in isbns:
        fields.append(
            '<marc:datafield tag="020" ind1=" " ind2=" ">'
            f'<marc:subfield code="a">{raw}</marc:subfield>'
            '<marc:subfield code="c">copy 1</marc:subfield>'
            "</marc:datafield>")
    return ("<marc:record>" + "".join(fields) + "</marc:record>")


def hyphenate(i13: str) -> str:
    return f"{i13[:3]}-{i13[3:]}"


MARC_DOC = ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<marc:collection xmlns:marc="{MARC_NS}">'
            + marc_record("DNB-1", [isbn13_to_10(isbn13_for(1)),
                                     hyphenate(isbn13_for(2)) + " (hbk.)"])
            + marc_record("DNB-2", ["9783110456789"])          # not in workset
            + marc_record("DNB-3", [isbn13_for(4)])
            + "</marc:collection>")


def test_parse_holdings_bulk_marc_fixture(holdings_db, tmp_path):
    conn, _ = holdings_db
    xml = tmp_path / "dnb.mrc.xml"
    xml.write_text(MARC_DOC, encoding="utf-8")
    stats = m5.parse_holdings_bulk(conn, "dnb", [xml], progress_every=0)
    assert stats["records_read"] == 3
    assert stats["isbn_matches"] == 3        # isbn10-folded i1, hyphen13 i2, plain i4
    rows = {(r["isbn13"], r["institution"], r["record_id"])
            for r in conn.execute("SELECT * FROM holdings")}
    assert rows == {(isbn13_for(1), "dnb", "DNB-1"),
                    (isbn13_for(2), "dnb", "DNB-1"),   # same record, 2 ISBNs
                    (isbn13_for(4), "dnb", "DNB-3")}
    # idempotent: rerun changes nothing
    stats2 = m5.parse_holdings_bulk(conn, "dnb", [xml], progress_every=0)
    assert stats2["holdings_rows"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM holdings"
                        ).fetchone()["c"] == 3


def test_parse_holdings_bulk_gz_and_zip_paths(holdings_db, tmp_path):
    """Real feeds ship as .xml.gz (dnb/loc) or weekly .zip archives (ndl) —
    both paths must stream the same records."""
    conn, _ = holdings_db
    gz = tmp_path / "dnb_all_dnbmarc.1.mrc.xml.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(MARC_DOC)
    stats = m5.parse_holdings_bulk(conn, "dnb", [gz], progress_every=0)
    assert stats["holdings_rows"] == 3

    zconn = m4.connect(tmp_path / "ndl.db")
    m5.ensure_schema(zconn)
    for i in range(1, 4):
        zconn.execute("INSERT INTO enrich_workset VALUES (?,?,?,?,?,?)",
                      (isbn13_for(i), "/works/N%d" % i, 1, 1, None, m5.now()))
        zconn.execute("INSERT INTO enrich_status (isbn13) VALUES (?)",
                      (isbn13_for(i),))
    zconn.commit()
    zp = tmp_path / "jmo202637.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("readme.txt", "not marc")
        zf.writestr("jmo202637.xml", MARC_DOC)
    stats = m5.parse_holdings_bulk(zconn, "ndl", [zp], progress_every=0)
    assert stats["records_read"] == 3
    assert {(r["isbn13"], r["institution"]) for r in
            zconn.execute("SELECT isbn13, institution FROM holdings")} == \
        {(isbn13_for(1), "ndl"), (isbn13_for(2), "ndl")}
    zconn.close()


# ------------------------------------------------------------ SRU stubs
class FakeSRUResponse:
    def __init__(self, body: str, status_code: int = 200):
        self.status_code = status_code
        self.text = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class FakeSRU:
    """Serves canned shape-faithful SRU envelopes keyed by query; records
    calls + sleeps (rate-discipline assertions)."""

    def __init__(self, by_query: dict[str, str] | FakeSRUResponse):
        self.by_query = {k: (v if isinstance(v, FakeSRUResponse) else
                             FakeSRUResponse(v))
                         for k, v in by_query.items()}
        self.calls: list[dict] = []
        self.sleeps: list[float] = []

    def get(self, url, params):
        self.calls.append({"url": url, "params": params})
        return self.by_query.get(params["query"],
                                 FakeSRUResponse(sru_envelope("", n=0)))

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)


def sru_envelope(records_xml: str, n: int | None = None) -> str:
    n = len(records_xml) if n is None else n
    return (f'<searchRetrieveResponse xmlns="{SRW_NS}">'
            f"<numberOfRecords>{n}</numberOfRecords>"
            f"<records>{records_xml}</records></searchRetrieveResponse>")


def sru_hit_marc(rec_id: str) -> str:
    """DNB-style: SRW record wrapping MARC21-slim with 001."""
    return (f'<record xmlns="{SRW_NS}"><recordSchema>MARC21-xml</recordSchema>'
            f'<recordData><record xmlns="{MARC_NS}">'
            f'<controlfield tag="001">{rec_id}</controlfield>'
            "</record></recordData></record>")


def test_enrich_holdings_dnb_sru(holdings_db):
    conn, _ = holdings_db
    i1, i2, i3 = isbn13_for(1), isbn13_for(2), isbn13_for(3)
    http = FakeSRU({
        f"isbn={i1}": sru_envelope(sru_hit_marc("965928276")),
        f"isbn={i2}": sru_envelope("", n=0),        # national-scope miss
    })
    out = m5.enrich_holdings(conn, "dnb", 2, get=http.get, sleep=http.sleep)
    assert out == {"institution": "dnb", "budget": 2, "queried": 2,
                   "held": 1, "missed": 1, "failed": 0}
    assert all(c["url"] == "https://services.dnb.de/sru/dnb"
               for c in http.calls)
    assert http.calls[0]["params"]["version"] == "1.1"
    assert http.calls[0]["params"]["recordSchema"] == "MARC21-xml"
    assert http.calls[0]["params"]["maximumRecords"] == 1
    assert all(s == 0.5 for s in http.sleeps)      # <=2 rps discipline
    rows = {(r["isbn13"], r["institution"], r["record_id"])
            for r in conn.execute("SELECT * FROM holdings")}
    assert rows == {(i1, "dnb", "965928276")}
    # resume: rerun skips the held i1 row entirely; misses (i2) re-query
    http2 = FakeSRU({f"isbn={i3}": sru_envelope(sru_hit_marc("X3"), n=1)})
    out2 = m5.enrich_holdings(conn, "dnb", 10, get=http2.get,
                              sleep=http2.sleep)
    assert out2["queried"] == 10 and out2["held"] == 1
    queried = [c["params"]["query"] for c in http2.calls]
    assert f"isbn={i1}" not in queried          # held row skipped
    assert f"isbn={i3}" in queried and len(queried) == 10
    row3 = conn.execute("SELECT record_id FROM holdings WHERE isbn13=?",
                        (i3,)).fetchone()
    assert row3["record_id"] == "X3"


def test_enrich_holdings_loc_plain_http_mods(holdings_db):
    """lx2.loc.gov:210 serves plain HTTP only (TLS broken on that port —
    verified 2026-10-02); the http:// scheme must be accepted and a MODS
    recordIdentifier captured as record_id."""
    conn, _ = holdings_db
    mods = (f'<record xmlns="{SRW_NS}"><recordSchema>mods</recordSchema>'
            f'<recordData><mods xmlns="{MODS_NS}"><recordInfo>'
            f"<recordIdentifier>12345678</recordIdentifier>"
            "</recordInfo></mods></recordData></record>")
    http = FakeSRU({"bath.isbn=" + isbn13_for(5):
                    sru_envelope(mods, n=1)})
    out = m5.enrich_holdings(conn, "loc", 5, get=http.get, sleep=http.sleep)
    assert out["held"] == 1 and out["missed"] == 4   # i1-i4 miss, i5 hits
    assert http.calls[-1]["url"] == "http://lx2.loc.gov:210/lcdb"
    assert all(c["url"].startswith("http://") for c in http.calls)
    row = conn.execute("SELECT record_id FROM holdings").fetchone()
    assert row["record_id"] == "12345678"


def test_enrich_holdings_bnf_and_ndl_shapes(holdings_db):
    conn, _ = holdings_db
    # bnf: version 1.2, 'bib.isbn all "..."' CQL, unimarcxchange payload
    bnf_rec = (f'<record xmlns="{SRW_NS}"><recordSchema>unimarcxchange'
               f"</recordSchema><recordData/>"
               f'<recordIdentifier>FRBNF1</recordIdentifier></record>')
    http = FakeSRU({f'bib.isbn all "{isbn13_for(6)}"':
                    sru_envelope(bnf_rec, n=1)})
    out = m5.enrich_holdings(conn, "bnf", 6, get=http.get, sleep=http.sleep)
    assert out["held"] == 1
    assert http.calls[-1]["url"] == "https://catalogue.bnf.fr/api/SRU"
    assert http.calls[-1]["params"]["version"] == "1.2"
    assert (conn.execute("SELECT record_id FROM holdings "
                         "WHERE institution='bnf'").fetchone()
            ["record_id"] == "FRBNF1")
    # ndl: keyless dcndl_v3, no version param
    ndl_rec = (f'<record xmlns="{SRW_NS}"><recordSchema>dcndl_v3'
               f'</recordSchema><recordData><rdf:RDF '
               f'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<dcterms:identifier xmlns:dcterms="http://purl.org/dc/terms/"'
               f">R100000136-I1970023484894665009</dcterms:identifier>"
               "</rdf:RDF></recordData></record>")
    http2 = FakeSRU({"isbn=" + isbn13_for(7):
                     sru_envelope(ndl_rec, n=2)})
    out2 = m5.enrich_holdings(conn, "ndl", 7, get=http2.get,
                              sleep=http2.sleep)
    assert out2["held"] == 1
    assert http2.calls[-1]["url"] == "https://ndlsearch.ndl.go.jp/api/sru"
    assert "version" not in http2.calls[-1]["params"]
    # dcndl identifier is not a 001/recordIdentifier -> record_id NULL is fine
    assert conn.execute("SELECT COUNT(*) c FROM holdings "
                        "WHERE institution='ndl'").fetchone()["c"] == 1


def test_enrich_holdings_budget_cap_and_failure(holdings_db):
    conn, _ = holdings_db
    http = FakeSRU({f"isbn={isbn13_for(n)}": sru_envelope("", n=0)
                    for n in range(1, 13)})
    out = m5.enrich_holdings(conn, "dnb", 4, get=http.get, sleep=http.sleep)
    assert out["queried"] == 4 and out["missed"] == 4   # budget honored
    assert len(http.calls) == 4
    # deterministic isbn13 ASC selection
    sel = [c["params"]["query"].split("=")[1] for c in http.calls]
    assert sel == sorted([isbn13_for(n) for n in (1, 2, 3, 4)])
    # a 500 response fails the row without raising (retried next run)
    http2 = FakeSRU({f"isbn={isbn13_for(n)}":
                     FakeSRUResponse("boom", status_code=500)
                     for n in (1, 2)})
    out2 = m5.enrich_holdings(conn, "dnb", 2, get=http2.get,
                              sleep=http2.sleep)
    assert out2["failed"] == 2 and out2["held"] == 0


def test_enrich_holdings_rejects_unknown_institution(holdings_db):
    conn, _ = holdings_db
    with pytest.raises(ValueError):
        m5.enrich_holdings(conn, "bl", 1)   # blocked / not an SRU target


# ------------------------------------------------------------ summary + export
def test_assign_holdings_summary_joined_codes(holdings_db):
    conn, _ = holdings_db
    conn.executemany(
        "INSERT INTO holdings (isbn13, institution, record_id) "
        "VALUES (?,?,?)",
        [(isbn13_for(1), "loc", "L1"), (isbn13_for(1), "dnb", "D1"),
         (isbn13_for(2), "bnf", "B2")])
    conn.commit()
    stats = m5.assign_holdings_summary(conn)
    assert stats["rows"] == 12 and stats["with_holdings"] == 2
    assert stats["by_institution"] == {"bnf": 1, "dnb": 1, "loc": 1}
    got = dict(conn.execute("SELECT isbn13, holdings FROM enrich_status"
                            ).fetchall())
    assert got[isbn13_for(1)] == "dnb,loc"      # joined sorted codes
    assert got[isbn13_for(2)] == "bnf"
    assert got[isbn13_for(3)] == ""             # empty when none
    # idempotent recompute
    m5.assign_holdings_summary(conn)
    assert got == dict(conn.execute(
        "SELECT isbn13, holdings FROM enrich_status").fetchall())
    # status/custody rules untouched by holdings
    st = conn.execute("SELECT status, custody FROM enrich_status "
                      "WHERE isbn13=?", (isbn13_for(1),)).fetchone()
    assert st["status"] is None and st["custody"] is None


def test_export_holdings_column_after_custody(holdings_db, tmp_path):
    conn, _ = holdings_db
    conn.executemany(
        "INSERT INTO holdings (isbn13, institution, record_id) "
        "VALUES (?,?,?)",
        [(isbn13_for(1), "loc", "L1"), (isbn13_for(1), "dnb", "D1"),
         (isbn13_for(2), "ndl", "N2")])
    conn.commit()
    m5.assign_holdings_summary(conn)
    csv_path, md_path = tmp_path / "ws.csv", tmp_path / "ws.md"
    stats = m5.export_workset(conn, csv_path, md_path)
    assert stats["exported"] == 12
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    import csv as _csv
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(_csv.reader(fh))
    hdr = rows[0]
    assert hdr.index("custody_physical") == hdr.index("custody") + 1
    assert hdr.index("holdings") == hdr.index("custody_physical") + 1
    data = {r[0]: r[hdr.index("holdings")] for r in rows[1:]
            if not r[0].startswith("#")}
    assert data[isbn13_for(1)] == "dnb,loc"
    assert data[isbn13_for(2)] == "ndl"
    assert data[isbn13_for(3)] == ""
    md = md_path.read_text(encoding="utf-8")
    assert "holdings" in md and "dnb,loc" in md


# ------------------------------------------------------------ CLI smoke
def test_cli_holdings_subcommands(holdings_db, tmp_path, capsys):
    conn, db_path = holdings_db
    db = str(tmp_path / "holdings.db")
    
    xml = tmp_path / "dnb.mrc.xml"
    xml.write_text(MARC_DOC, encoding="utf-8")
    i5 = isbn13_for(5)
    http = FakeSRU({"bath.isbn=" + i5: sru_envelope(sru_hit_marc("LC1"),
                                                    n=1)})
    real_get = m5._http_get
    m5._http_get = http.get          # enrich_holdings resolves this lazily
    try:
        assert cli_main(["--db", db, "parse-holdings-bulk",
                         "--institution", "dnb", "--file", str(xml)]) == 0
        assert cli_main(["--db", db, "enrich-holdings",
                         "--institution", "loc", "--budget", "5"]) == 0
        assert cli_main(["--db", db, "assign-holdings-summary"]) == 0
    finally:
        m5._http_get = real_get
    out = capsys.readouterr().out
    assert "parse-holdings-bulk" in out and "holdings row(s)" in out
    assert "enrich-holdings" in out and "held=1" in out
    assert "assign-holdings-summary" in out
    # loc row landed via the CLI enrich path; summary derived both codes
    rows = {(r["isbn13"], r["institution"]) for r in
            conn.execute("SELECT isbn13, institution FROM holdings")}
    assert rows == {(isbn13_for(1), "dnb"), (isbn13_for(2), "dnb"),
                    (isbn13_for(4), "dnb"), (i5, "loc")}
    # fetch rejects bnf (SRU-first, no bulk MARC set) cleanly at parse time
    with pytest.raises(SystemExit) as exc:
        cli_main(["--db", db, "fetch-holdings-bulk", "--institution", "bnf"])
    assert exc.value.code == 2


def test_cli_parse_and_export_holdings_csv(holdings_db, tmp_path, capsys):
    conn, db_path = holdings_db
    db = str(tmp_path / "holdings.db")
    
    gz = tmp_path / "BooksAll.2016.part01.xml.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(MARC_DOC.replace("DNB-", "LC-"))
    csv_path = tmp_path / "ws.csv"
    for argv in [
        ["parse-holdings-bulk", "--institution", "loc", "--file", str(gz)],
        ["assign-holdings-summary"],
        ["export-list", "--workset", "--csv", str(csv_path)],
    ]:
        assert cli_main(["--db", db] + argv) == 0
    hdr = csv_path.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert hdr.index("custody_physical") == hdr.index("custody") + 1
    assert hdr.index("holdings") == hdr.index("custody_physical") + 1


# ------------------------------------------------------------ glob --file (2026-10-03)
def test_parse_holdings_bulk_expands_glob_patterns(holdings_db, tmp_path):
    """Shell-unexpanded globs (quoted '/path/BooksAll.2016.*.xml.gz')
    reaching the process literally must be expanded by the parser
    (postmortem 2026-10-03: literal pattern silently parsed 0 records)."""
    conn, _ = holdings_db
    for part in (1, 2):
        gz = tmp_path / f"BooksAll.2016.part{part:02d}.xml.gz"
        with gzip.open(gz, "wt", encoding="utf-8") as fh:
            fh.write(MARC_DOC.replace("DNB-", f"LC{part}-"))
    pattern = str(tmp_path / "BooksAll.2016.*.xml.gz")
    stats = m5.parse_holdings_bulk(conn, "loc", [pattern], progress_every=0)
    assert stats["records_read"] == 6            # both parts parsed
    assert stats["isbn_matches"] == 6            # 3 matches per part
    assert stats["holdings_rows"] == 3           # same ISBNs -> idempotent upsert


def test_parse_holdings_bulk_glob_no_match_errors(holdings_db, tmp_path):
    conn, _ = holdings_db
    pattern = str(tmp_path / "BooksAll.2016.part99.xml.gz")
    with pytest.raises(FileNotFoundError) as exc:
        m5.parse_holdings_bulk(conn, "loc", [pattern], progress_every=0)
    assert pattern in str(exc.value)
    # CLI surfaces it cleanly (rc 2), matching fetch-holdings-bulk style
    db = str(tmp_path / "holdings.db")
    rc = cli_main(["--db", db, "parse-holdings-bulk", "--institution", "loc",
                   "--file", pattern])
    assert rc == 2
