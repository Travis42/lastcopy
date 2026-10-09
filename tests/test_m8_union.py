"""M8 — union-catalog (K10plus GVK) holdings tests (SPEC-M8-UNION-HOLDINGS).

Covers: the PICA-XML parser against the REAL fixture response excerpt
(tests/fixtures/k10plus_pica.xml, fetched live 2026-10-09 — holding codes
are 209A $B, PPN is 003@ $0), enrich-holdings --institution k10plus row
shape incl. space-joined detail codes, resume-skip + budget stop, custody
re-derivation counting k10plus as an institution (wild->single), report
deltas, and CLI smoke with a fake get.  No network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lastcopy import m4, m5
from lastcopy.cli import main as cli_main

FIXTURE = Path(__file__).parent / "fixtures" / "k10plus_pica.xml"
SRW_NS = "http://www.loc.gov/zing/srw/"


def isbn13_for(n: int, prefix: str = "9780") -> str:
    core = prefix + f"{n:08d}"
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


@pytest.fixture()
def union_db(tmp_path):
    conn = m4.connect(tmp_path / "union.db")
    m5.ensure_schema(conn)
    for i in range(1, 13):
        conn.execute(
            "INSERT INTO enrich_workset (isbn13, work_key, edition_count, "
            "score, oclc, created_at) VALUES (?,?,1,?,NULL,?)",
            (isbn13_for(i), f"/works/U{i}", 10 - i, m5.now()))
        conn.execute("INSERT INTO enrich_status (isbn13) VALUES (?)",
                     (isbn13_for(i),))
    conn.commit()
    yield conn, tmp_path
    conn.close()


class FakeResponse:
    def __init__(self, body: str, status_code: int = 200):
        self.status_code = status_code
        self.text = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class FakeSRU:
    """Serves canned SRU envelopes keyed by query; records calls/sleeps."""

    def __init__(self, by_query: dict):
        self.by_query = by_query
        self.calls: list[dict] = []
        self.sleeps: list[float] = []

    def get(self, url, params):
        self.calls.append({"url": url, "params": params})
        return self.by_query.get(params["query"],
                                 FakeResponse(f"<boom>{params['query']}</boom>"))

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)


# ------------------------------------------------------------ parser (fixture)
def test_parse_pica_holdings_real_fixture():
    n, ppn, codes = m5._parse_pica_holdings(
        FIXTURE.read_text(encoding="utf-8"))
    assert n == 2                                   # both P&P editions
    assert ppn == "1769930981"                      # first record's 003@ $0
    assert codes == ["751", "291/309", "15", "16/24", "77/004"]


def test_parse_pica_holdings_bad_xml():
    assert m5._parse_pica_holdings("not xml <<<") == (0, None, [])


# ------------------------------------------------------------ enrich-holdings
def test_enrich_holdings_k10plus_row_shape(union_db):
    conn, _ = union_db
    i1 = isbn13_for(1)
    http = FakeSRU({"pica.isb=" + i1: FakeResponse(
        FIXTURE.read_text(encoding="utf-8"))})
    out = m5.enrich_holdings(conn, "k10plus", 3, get=http.get,
                             sleep=http.sleep)
    assert out == {"institution": "k10plus", "budget": 3, "queried": 3,
                   "held": 1, "missed": 2, "failed": 0}
    # endpoint shape: version 1.1, picaxml schema, 1 record max
    c = http.calls[0]
    assert c["url"] == "https://sru.k10plus.de/gvk"
    assert c["params"]["version"] == "1.1"
    assert c["params"]["recordSchema"] == "picaxml"
    assert c["params"]["maximumRecords"] == 1
    assert all(s == 0.5 for s in http.sleeps)       # <=2 rps discipline
    row = conn.execute("SELECT * FROM holdings WHERE institution='k10plus'"
                       ).fetchone()
    assert row["isbn13"] == i1
    assert row["record_id"] == "1769930981"
    assert row["detail"] == "751 291/309 15 16/24 77/004"   # space-joined


def test_enrich_holdings_k10plus_resume_and_budget(union_db):
    conn, _ = union_db
    i1, i2, i3 = isbn13_for(1), isbn13_for(2), isbn13_for(3)
    # seed i1 as already held -> resume must skip it entirely
    conn.execute("INSERT INTO holdings (isbn13, institution, record_id, "
                 "detail) VALUES (?,?,?,?)", (i1, "k10plus", "X", "DE-6"))
    conn.commit()
    body = FIXTURE.read_text(encoding="utf-8")
    http = FakeSRU({"pica.isb=" + i2: FakeResponse(body)})
    out = m5.enrich_holdings(conn, "k10plus", 2, get=http.get,
                             sleep=http.sleep)
    assert out["budget"] == 2 and out["held"] == 1 and out["missed"] == 1
    assert len(http.calls) == 2                     # budget stop honored
    queried = [c["params"]["query"] for c in http.calls]
    assert "pica.isb=" + i1 not in queried          # held row skipped
    assert queried[0] == "pica.isb=" + i2           # isbn13 ASC selection
    assert queried[1] == "pica.isb=" + i3
    # national rows keep detail NULL (shape untouched)
    conn.execute("INSERT INTO holdings (isbn13, institution, record_id) "
                 "VALUES (?,?,?)", (i1, "dnb", "D1"))
    conn.commit()
    dnb = conn.execute("SELECT detail FROM holdings WHERE institution='dnb'"
                       ).fetchone()
    assert dnb["detail"] is None


# ------------------------------------------------------------ custody re-derive
def test_custody_rederivation_counts_k10plus(union_db):
    """wild->single when k10plus is the ONLY institution holding the book;
    single->multi when it joins one national library."""
    conn, _ = union_db
    i1, i2, i3 = isbn13_for(1), isbn13_for(2), isbn13_for(3)
    conn.executemany(
        "INSERT INTO holdings (isbn13, institution, record_id, detail) "
        "VALUES (?,?,?,?)",
        [(i1, "k10plus", "K1", "15 16/24"),          # only k10plus
         (i2, "k10plus", "K2", "751"),               # + national
         (i2, "dnb", "D2", None),
         (i3, "dnb", "D3", None)])                   # national only
    conn.commit()
    stats = m5.assign_holdings_summary(conn)
    assert stats["by_institution"].get("k10plus") == 2
    rows = {r["isbn13"]: r for r in conn.execute(
        "SELECT isbn13, custody_physical, holdings FROM enrich_status")}
    assert rows[i1]["custody_physical"] == "single"   # wild -> single
    assert rows[i1]["holdings"] == "k10plus"
    assert rows[i2]["custody_physical"] == "multi"    # single -> multi
    assert rows[i2]["holdings"] == "dnb,k10plus"
    assert rows[i3]["custody_physical"] == "single"
    assert rows[isbn13_for(4)]["custody_physical"] == "wild"


# ------------------------------------------------------------ report
def test_union_holdings_report_deltas(union_db):
    conn, _ = union_db
    i1, i2, i3, i4 = (isbn13_for(n) for n in (1, 2, 3, 4))
    conn.executemany(
        "INSERT INTO holdings (isbn13, institution, record_id, detail) "
        "VALUES (?,?,?,?)",
        [(i1, "k10plus", "K1", "15"),                # only k10plus
         (i2, "k10plus", "K2", "16/24"),             # one other
         (i2, "loc", "L2", None),
         (i3, "k10plus", "K3", "751"),               # two others
         (i3, "dnb", "D3", None), (i3, "loc", "L3", None),
         (i4, "dnb", "D4", None)])                   # no k10plus row
    conn.commit()
    rep = m5.union_holdings_report(conn)
    assert rep["institution"] == "k10plus"
    assert rep["books_with_union_holding"] == 3
    assert rep["with_library_detail"] == 3
    assert rep["deltas"] == {"wild_to_single": 1, "single_to_multi": 1,
                             "already_multi": 1}
    assert rep["rescued_total"] == 2


# ------------------------------------------------------------ CLI smoke
def test_cli_k10plus_and_union_report(union_db, tmp_path, capsys):
    conn, _ = union_db
    db = str(tmp_path / "union.db")
    body = FIXTURE.read_text(encoding="utf-8")
    http = FakeSRU({"pica.isb=" + isbn13_for(1): FakeResponse(body)})
    real_get = m5._http_get
    m5._http_get = http.get
    try:
        assert cli_main(["--db", db, "enrich-holdings",
                         "--institution", "k10plus", "--budget", "2"]) == 0
    finally:
        m5._http_get = real_get
    # seed one k10plus-only rescue + one multi, then derive + report via CLI
    conn.executemany(
        "INSERT INTO holdings (isbn13, institution, record_id, detail) "
        "VALUES (?,?,?,?)",
        [(isbn13_for(2), "k10plus", "K2", "751"),
         (isbn13_for(3), "k10plus", "K3", None),
         (isbn13_for(3), "dnb", "D3", None)])
    conn.commit()
    assert cli_main(["--db", db, "assign-holdings-summary"]) == 0
    assert cli_main(["--db", db, "union-holdings-report"]) == 0
    out = capsys.readouterr().out
    assert "enrich-holdings" in out and "held=1" in out
    assert "union-holdings-report" in out and "wild->single=2" in out
    assert "rescued=3" in out
    # enrich-holdings CLI accepts k10plus; unknown still rejected at parse
    with pytest.raises(SystemExit):
        cli_main(["--db", db, "enrich-holdings",
                  "--institution", "sudoc", "--budget", "1"])
