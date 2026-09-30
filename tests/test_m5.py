"""M5 — bulk enrichment + Book Red List tests (SPEC-M5-ENRICHMENT).

Covers: mini hathifile fixture (mixed ISBN formats, OCLC joins, allow/deny
precedence), OCLC backfill first-record-wins, IA plan generation (exact
element counts, deterministic order) + stubbed-HTTP executor (faithful
requests-style response objects, 429 backoff, resume/skip-done), the full
status rule table incl. precedence and DD, and export-list --workset.
No network access.
"""

from __future__ import annotations

import gzip
import json

import pytest

from lastcopy import m4, m5
from lastcopy.cli import main as cli_main
from lastcopy.isbn import isbn13_to_10

TS = "2009-03-27T12:00:00.000000+00:00"


def isbn13_for(n: int, prefix: str = "9780") -> str:
    core = prefix + f"{n:08d}"
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def dump_line(type_: str, key: str, obj: dict) -> str:
    return "\t".join([type_, key, "1", TS, json.dumps(obj, ensure_ascii=False)])


def write_gz(path, lines) -> str:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


# ------------------------------------------------------------ fixture pipeline
# 6 works / 12 ISBN-keyed editions: edition_count 1,2,3,4,1,1
WORKS = [
    dump_line("/type/work", f"/works/OW{i}", {"title": f"Work {i}"})
    for i in range(1, 7)
]


def _ed(key: str, isbn13: str, oclc: str | None, work: str) -> str:
    obj = {"title": f"Ed {key}", "publish_date": "1930", "isbn_13": [isbn13],
           "works": [{"key": work}]}
    if oclc:
        obj["oclc_numbers"] = [oclc]
    return dump_line("/type/edition", f"/books/{key}", obj)


EDITIONS = [
    _ed("E1", isbn13_for(1), "111", "/works/OW1"),
    _ed("E2a", isbn13_for(2), "222", "/works/OW2"),
    _ed("E2b", isbn13_for(3), "222", "/works/OW2"),
    _ed("E3a", isbn13_for(4), "333", "/works/OW3"),
    _ed("E3b", isbn13_for(5), "333", "/works/OW3"),
    _ed("E3c", isbn13_for(6), "333", "/works/OW3"),
    _ed("E4a", isbn13_for(7), "444", "/works/OW4"),
    _ed("E4b", isbn13_for(8), "444", "/works/OW4"),
    _ed("E4c", isbn13_for(9), "444", "/works/OW4"),
    _ed("E4d", isbn13_for(10), "444", "/works/OW4"),
    _ed("E5", isbn13_for(11), None, "/works/OW5"),   # no OCLC anywhere
    _ed("E6", isbn13_for(12), "666", "/works/OW6"),
]


@pytest.fixture()
def m5_db(tmp_path):
    write_gz(tmp_path / "works.txt.gz", WORKS)
    write_gz(tmp_path / "editions.txt.gz", EDITIONS)
    conn = m4.connect(tmp_path / "m5.db")
    m4.ingest_works(conn, tmp_path / "works.txt.gz", tmp_path / "works.txt.gz",
                    progress_every=0)
    m4.ingest_editions(conn, file=tmp_path / "editions.txt.gz", progress_every=0)
    m4.gen_candidates(conn, max_editions=5)
    yield conn, tmp_path
    conn.close()


# ------------------------------------------------------------------ stage 0
def test_backfill_oclc_multi_isbn_first_record_wins(m5_db):
    conn, tmp_path = m5_db
    # same dump plus a LATER record for isbn13_for(1) with a different OCLC
    # and a record whose ISBN never made the candidates/workset cut
    dup = write_gz(tmp_path / "editions2.txt.gz", EDITIONS + [
        _ed("E1LATE", isbn13_for(1), "999", "/works/OW1"),
        _ed("ENEW", isbn13_for(30), "777", "/works/OW1"),
    ])
    stats = m5.backfill_oclc(conn, dup, progress_every=0)
    assert stats["workset"] == 12
    ws = dict(conn.execute("SELECT isbn13, oclc FROM enrich_workset").fetchall())
    # multi-ISBN records share one OCLC, stored per isbn
    assert ws[isbn13_for(2)] == "222" and ws[isbn13_for(3)] == "222"
    # first-record-wins: E1 (oclc 111) precedes E1LATE (oclc 999) in the dump
    assert ws[isbn13_for(1)] == "111"
    # no OCLC on OW5's edition -> stays NULL
    assert ws[isbn13_for(11)] is None
    # enrich_status seeded, one row per workset ISBN
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status"
                        ).fetchone()["c"] == 12


def test_workset_deterministic_top_n(m5_db):
    conn, _ = m5_db
    # every candidate has identical shape except score; workset order is
    # score DESC, isbn13 ASC — idempotent rebuild
    assert m5.build_workset(conn) == 12
    rows1 = conn.execute("SELECT isbn13 FROM enrich_workset "
                         "ORDER BY score DESC, isbn13 ASC").fetchall()
    m5.build_workset(conn)
    rows2 = conn.execute("SELECT isbn13 FROM enrich_workset "
                         "ORDER BY score DESC, isbn13 ASC").fetchall()
    assert [r["isbn13"] for r in rows1] == [r["isbn13"] for r in rows2]


# ------------------------------------------------------------------ stage 1
def ht_line(access: str, oclc: str, isbn: str) -> str:
    cols = ["x"] * len(m5.HT_COLS)
    cols[0] = "htid-placeholder"
    cols[m5.HT_I_ACCESS] = access
    cols[m5.HT_I_OCLC] = oclc
    cols[m5.HT_I_ISBN] = isbn
    return "\t".join(cols)


MINI_HATHIFILE = "\n".join([
    ht_line("deny", "111", isbn13_to_10(isbn13_for(1))),       # isbn10 form of #1, deny
    ht_line("allow", "111", isbn13_for(1)),                    # same isbn, allow -> wins
    ht_line("deny", "222", isbn13_for(2)),                     # deny-only row (isbn match)
    ht_line("allow", "333", isbn13_for(99)),                   # isbn not in workset...
    ht_line("deny", "999", isbn13_for(98)),                    # unrelated entirely
    ht_line("allow", "0", "9780000000000"),                    # invalid-checksum isbn, ignored
    ht_line("allow", "555", isbn13_for(97)),                   # unrelated entirely
    ht_line("allow", "666", f"{isbn13_for(12)}, 9780000000001"),  # mixed valid+invalid
    ht_line("deny", "888", isbn13_for(11)),                    # OW5 (no workset oclc) by isbn
    ht_line("allow", "999", f"junk; {isbn13_for(6)}"),         # junk-separated mixed formats
]) + "\n"


def test_enrich_ht_mini_hathifile(m5_db, tmp_path):
    conn, _ = m5_db
    m5.backfill_oclc(conn, tmp_path / "editions.txt.gz", progress_every=0)
    hf = tmp_path / "hathifile.tsv"
    hf.write_text(MINI_HATHIFILE, encoding="utf-8")
    stats = m5.enrich_ht(conn, hf)
    assert stats["lines_read"] == 10
    got = dict(conn.execute("SELECT isbn13, ht_access FROM enrich_status"
                            ).fetchall())
    assert got[isbn13_for(1)] == "allow"      # allow beats deny (precedence)
    assert got[isbn13_for(2)] == "deny"       # deny-only, isbn match
    assert got[isbn13_for(3)] == "deny"       # OCLC join (oclc 222), deny-only row
    assert got[isbn13_for(4)] == "allow"      # OCLC join (oclc 333) -> allow
    assert got[isbn13_for(6)] == "allow"      # junk-separated isbn still matched
    assert got[isbn13_for(12)] == "allow"     # valid isbn amid invalid one
    assert got[isbn13_for(11)] == "deny"      # matched by isbn despite no workset oclc
    assert got[isbn13_for(7)] is None         # never matched
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%ht%'").fetchone()["c"] == 12


# ------------------------------------------------------------------ stage 2
WD_RESULTS = {
    "results": {"bindings": [
        {"isbn": {"value": isbn13_for(9)},
         "url": {"value": "https://www.gutenberg.org/ebooks/9"}},
        {"isbn": {"value": isbn13_to_10(isbn13_for(8))},
         "url": {"value": "https://example.org/full8"}},
        {"isbn": {"value": isbn13_for(7)},
         "url": {"value": ""}},                       # empty link -> no fulltext
        {"isbn": {"value": isbn13_for(55)},
         "url": {"value": "https://x"}},              # not in workset
        {"title": {"value": "no isbn binding"}},
    ]}}


def test_enrich_wikidata_offline_parse(m5_db, tmp_path):
    conn, _ = m5_db
    m5.backfill_oclc(conn, tmp_path / "editions.txt.gz", progress_every=0)
    p = tmp_path / "wd.json"
    p.write_text(json.dumps(WD_RESULTS), encoding="utf-8")
    stats = m5.enrich_wikidata(conn, p)
    assert stats["wd_fulltext"] == 2      # isbn13 direct + isbn10-converted
    got = dict(conn.execute("SELECT isbn13, wd_fulltext FROM enrich_status"
                            ).fetchall())
    assert got[isbn13_for(9)] == 1
    assert got[isbn13_for(8)] == 1        # isbn10 form normalized onto #8
    assert got[isbn13_for(7)] == 0        # empty link
    assert got[isbn13_for(1)] == 0        # absent
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%wd%'").fetchone()["c"] == 12


# ------------------------------------------------------------------ stage 3
class FakeResponse:
    """Requests-style response object (shape-faithful stub)."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTP:
    """Serves canned IA advancedsearch responses; records calls + sleeps."""

    def __init__(self, script: list[FakeResponse] | dict[str, FakeResponse]):
        self.script = script if isinstance(script, list) else None
        self.by_query = script if isinstance(script, dict) else {}
        self.calls: list[dict] = []
        self.sleeps: list[float] = []

    def get(self, url, params):
        self.calls.append({"url": url, "params": params})
        if self.script is not None:
            return self.script.pop(0)
        return self.by_query[params["q"]]

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)


def _post_ht_wd(m5_db):
    conn, tmp_path = m5_db
    m5.backfill_oclc(conn, tmp_path / "editions.txt.gz", progress_every=0)
    hf = tmp_path / "hathifile.tsv"
    hf.write_text(MINI_HATHIFILE, encoding="utf-8")
    m5.enrich_ht(conn, hf)
    p = tmp_path / "wd.json"
    p.write_text(json.dumps(WD_RESULTS), encoding="utf-8")
    m5.enrich_wikidata(conn, p)
    return conn


# resolved by ht/wd after _post_ht_wd: i1,i4,i5,i6,i12 (ht allow), i8,i9 (wd)
# -> unresolved = i2,i3,i7,i10,i11 (5)
UNRESOLVED = sorted([isbn13_for(n) for n in (2, 3, 7, 10, 11)])


def test_ia_plan_exact_elements_and_determinism(m5_db):
    conn = _post_ht_wd(m5_db)
    stats = m5.build_ia_plan(conn, batch=30)
    assert stats["isbn_isbns"] == 5 and stats["oclc_isbns"] == 4
    assert stats["elements"] == 1 + 1   # ceil(5/30) + ceil(4/30)
    row = conn.execute("SELECT * FROM ia_plan WHERE element_idx=0").fetchone()
    assert row["kind"] == "isbn"
    assert json.loads(row["isbns"]) == UNRESOLVED   # deterministic isbn13 ASC
    assert row["query"] == " OR ".join(f"isbn:{v}" for v in UNRESOLVED)
    oclc_row = conn.execute(
        "SELECT * FROM ia_plan WHERE element_idx=1").fetchone()
    assert oclc_row["kind"] == "oclc"
    # regenerating the plan yields the identical sequence
    m5.build_ia_plan(conn, batch=30)
    snap = conn.execute("SELECT element_idx, kind, query, isbns FROM ia_plan "
                        "ORDER BY element_idx").fetchall()
    assert [(r["element_idx"], r["kind"], r["query"], r["isbns"]) for r in snap] == \
        [(0, "isbn", row["query"], row["isbns"]),
         (1, "oclc", oclc_row["query"], oclc_row["isbns"])]
    # batch=3: ceil(5/3)=2 isbn elements + ceil(4/3)=2 oclc elements
    stats = m5.build_ia_plan(conn, batch=3)
    assert stats["elements"] == 2 + 2
    assert json.loads(conn.execute(
        "SELECT isbns FROM ia_plan WHERE element_idx=0").fetchone()["isbns"]) \
        == UNRESOLVED[:3]


def test_ia_executor_records_hits_and_skips_done(m5_db):
    conn = _post_ht_wd(m5_db)
    m5.build_ia_plan(conn, batch=3)
    docs = [{"identifier": "scan-of-2", "isbn": [isbn13_for(2)]},
            {"identifier": "scan-of-both", "isbn": [isbn13_for(5), isbn13_for(7)]},
            {"identifier": "no-isbn-doc"}]
    http = FakeHTTP([FakeResponse(429),
                     FakeResponse(200, {"response": {"docs": docs}})])
    out = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep)
    # element 0 = [i2,i3,i7]: i2 hit directly, i7 via scan-of-both; i5 not in
    # this element so its doc isbn does not attribute
    assert out == {"element": 0, "skipped_done": False, "hits": 2}
    # rate discipline (1s) before each request + 2^0 backoff on the 429
    assert http.sleeps == [1.0, 1.0, 1.0]
    assert len(http.calls) == 2 and http.calls[0]["url"] == m5.IA_SEARCH_URL
    ia = dict(conn.execute("SELECT isbn13, ia_identifier FROM enrich_status"
                           ).fetchall())
    assert ia[isbn13_for(2)] == "scan-of-2"
    assert ia[isbn13_for(7)] == "scan-of-both"
    assert ia[isbn13_for(5)] is None         # not in this element
    assert ia[isbn13_for(3)] is None         # unhit stays NULL
    assert ia[isbn13_for(1)] is None         # already ht-allow -> never queried
    # plan row marked done; rerun skips it with zero HTTP
    out2 = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep)
    assert out2["skipped_done"] is True and out2["hits"] == 0
    assert len(http.calls) == 2
    # oclc element attribution via doc oclc field
    m5.build_ia_plan(conn, batch=30)
    # unresolved now i3,i10,i11 -> oclcs 222(i3), 444(i10); element 1 = oclc pass
    http2 = FakeHTTP([FakeResponse(200, {"response": {"docs": [
        {"identifier": "oclc-scan", "oclc": ["222"]}]}})])
    m5.execute_ia_element(conn, 1, get=http2.get, sleep=http2.sleep)
    ia = dict(conn.execute("SELECT isbn13, ia_identifier FROM enrich_status"
                           ).fetchall())
    assert ia[isbn13_for(3)] == "oclc-scan"  # i2 already hit -> not in plan
    assert conn.execute("SELECT done FROM ia_plan WHERE element_idx=1"
                        ).fetchone()["done"] == 1
    # ia checked recorded on every status row
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%ia%'"
                        ).fetchone()["c"] == 12


# ------------------------------------------------------------------ stage 4
NT_BASIS = "full digital exists"
CR_BASIS = "edition_count=1; no digital in HT/IA/WD"
DD_BASIS = "no signals resolvable"

STATUS_CASES = [
    # (name, edition_count, ht_access, wd_fulltext, ia_identifier, oclc,
    #  ia_checked, expected_status, basis_fragment)
    ("nt_via_ht", 1, "allow", 0, None, "111", True, "NT", NT_BASIS),
    ("nt_via_wd", 4, None, 1, None, "444", True, "NT", "wd fulltext"),
    ("nt_via_ia", 3, None, 0, "scanid", "333", True, "NT", "ia hit"),
    ("nt_beats_cr", 1, "allow", 1, "x", None, True, "NT", NT_BASIS),  # precedence
    ("cr_one_edition", 1, None, 0, None, "111", True, "CR", CR_BASIS),
    ("en_two", 2, None, 0, None, "222", True, "EN", "edition_count=2"),
    ("en_three", 3, None, 0, None, "333", True, "EN", "edition_count=3"),
    ("vu_four", 4, None, 0, None, "444", True, "VU", "edition_count=4"),
    ("vu_many", 9, None, 0, None, "444", True, "VU", "edition_count=9"),
    ("ht_deny_is_not_full", 1, "deny", 0, None, "111", True, "CR", CR_BASIS),
    ("dd_no_signals_no_oclc", 1, None, 0, None, None, True, "DD", DD_BASIS),
    ("dd_vu_shape", 4, None, 0, None, None, True, "DD", DD_BASIS),
    ("not_dd_when_oclc", 1, None, 0, None, "666", True, "CR", CR_BASIS),
    ("not_dd_when_ia_unchecked", 1, None, None, None, None, False, "CR", CR_BASIS),
]


@pytest.mark.parametrize("name,ec,ht,wd,ia,oclc,ia_checked,want,basis",
                         STATUS_CASES, ids=[c[0] for c in STATUS_CASES])
def test_status_rules_table_driven(tmp_path, name, ec, ht, wd, ia, oclc,
                                   ia_checked, want, basis):
    conn = m4.connect(tmp_path / "rules.db")
    m5.ensure_schema(conn)
    i13 = isbn13_for(1)
    conn.execute("INSERT INTO enrich_workset (isbn13, work_key, edition_count, "
                 "score, oclc, created_at) VALUES (?,?,?,?,?,?)",
                 (i13, "/works/X", ec, 5, oclc, m5.now()))
    conn.execute("INSERT INTO enrich_status (isbn13, ht_access, ia_identifier, "
                 "wd_fulltext, sources_checked) VALUES (?,?,?,?,?)",
                 (i13, ht, ia, wd, "ht,wd" + (",ia" if ia_checked else "")))
    stats = m5.assign_status(conn)
    row = conn.execute("SELECT status, status_basis FROM enrich_status"
                       ).fetchone()
    assert row["status"] == want
    assert basis in row["status_basis"]
    assert stats["counts"][want] == 1
    assert set(stats["counts"]) <= {"CR", "EN", "VU", "NT", "DD"}
    conn.close()


def test_assign_status_dd_requires_all_absent(tmp_path):
    """DD wins over edition-count rules but loses to any digital signal."""
    conn = m4.connect(tmp_path / "dd.db")
    m5.ensure_schema(conn)
    for n, (ht, wd, ia, oclc) in enumerate(
            [(None, 0, None, None), ("deny", 0, None, None),
             (None, 1, None, None), (None, 0, "x", None),
             (None, 0, None, "123")], start=1):
        conn.execute("INSERT INTO enrich_workset VALUES (?,?,?,?,?,?)",
                     (isbn13_for(n), None, 1, 1, oclc, m5.now()))
        conn.execute("INSERT INTO enrich_status (isbn13, ht_access, "
                     "ia_identifier, wd_fulltext, sources_checked) "
                     "VALUES (?,?,?,?, 'ht,wd,ia')",
                     (isbn13_for(n), ht, ia, wd))
    m5.assign_status(conn)
    got = dict(conn.execute("SELECT isbn13, status FROM enrich_status").fetchall())
    assert got[isbn13_for(1)] == "DD"   # nothing resolvable
    assert got[isbn13_for(2)] == "CR"   # ht deny row present = partial, not DD
    assert got[isbn13_for(3)] == "NT"   # wd fulltext
    assert got[isbn13_for(4)] == "NT"   # ia hit
    assert got[isbn13_for(5)] == "CR"   # has oclc -> queryable -> not DD
    conn.close()


# ------------------------------------------------------------------ export
def test_export_workset_header_order_note(m5_db, tmp_path, capsys):
    conn = _post_ht_wd(m5_db)
    m5.build_ia_plan(conn, batch=30)
    m5.execute_ia_element(
        conn, 0, get=lambda u, p: FakeResponse(200, {"response": {"docs": [
            {"identifier": "ia-for-2", "isbn": [isbn13_for(2)]}]}}),
        sleep=lambda s: None)
    stats = m5.assign_status(conn)
    assert stats["assigned"] == 12
    csv_path, md_path = tmp_path / "ws.csv", tmp_path / "ws.md"
    rc = cli_main(["--db", str(tmp_path / "m5.db"), "export-list", "--workset",
                   "--csv", str(csv_path), "--md", str(md_path)])
    assert rc == 0
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ("isbn13,title,author,year,language,edition_count,"
                        "score,status,status_basis")
    assert lines[-1] == f"# {m5.PROVISIONAL_NOTE}"
    body = [ln.split(",")[0] for ln in lines[1:-1]]
    st = dict(conn.execute("SELECT isbn13, status FROM enrich_status").fetchall())
    order = [st[i] for i in body]
    ranks = [m5.STATUS_ORDER[s] for s in order]
    assert ranks == sorted(ranks)          # severity CR,EN,VU,NT,DD ordering
    # within a status class: score DESC, isbn13 ASC
    for cls in ("CR", "EN", "VU", "NT", "DD"):
        group = [i for i in body if st[i] == cls]
        if len(group) > 1:
            scores = dict(conn.execute(
                "SELECT isbn13, score FROM enrich_workset").fetchall())
            keys = [(-scores[i], i) for i in group]
            assert keys == sorted(keys)
    md = md_path.read_text(encoding="utf-8")
    assert m5.PROVISIONAL_NOTE in md


def test_export_list_default_unchanged(m5_db, tmp_path):
    conn, _ = m5_db
    out = tmp_path / "plain.csv"
    m4.export_list(conn, top=5, csv_path=out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(m4.LIST_HEADER)


# ------------------------------------------------------------------ CLI smoke
def test_cli_m5_subcommands_render_and_run(m5_db, capsys):
    conn, tmp_path = m5_db
    db = str(tmp_path / "m5.db")
    hf = tmp_path / "hathifile.tsv"
    hf.write_text(MINI_HATHIFILE, encoding="utf-8")
    wd = tmp_path / "wd.json"
    wd.write_text(json.dumps(WD_RESULTS), encoding="utf-8")
    for argv in [
        ["backfill-oclc", "--file", str(tmp_path / "editions.txt.gz")],
        ["enrich-ht", "--hathifile", str(hf)],
        ["enrich-wikidata", "--results", str(wd)],
        ["enrich-ia", "--batch", "30", "--arraysize", "4"],
        ["assign-status"],
        ["export-list", "--workset", "--csv", str(tmp_path / "ws.csv")],
    ]:
        assert cli_main(["--db", db] + argv) == 0
    out = capsys.readouterr().out
    assert "backfill-oclc" in out and "enrich-ht" in out
    assert "assign-status" in out and "workset row(s)" in out
    # execute mode with an unknown element errors cleanly
    rc = cli_main(["--db", db, "enrich-ia", "--execute", "99"])
    assert rc != 0


def test_enrich_ht_gzipped_hathifile(tmp_path, monkeypatch):
    """Regression (2026-09-30): enrich_ht read .gz hathifiles as plain text,
    staged 0 rows, and silently marked everything CR. Real hathifiles are
    .txt.gz — the fixture must exercise the compressed path."""
    import gzip as _gzip
    import sqlite3
    from lastcopy import m5
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    m5.ensure_schema(conn)
    conn.execute("INSERT INTO enrich_workset (isbn13, work_key, edition_count, score) "
                 "VALUES ('9788081281587','/works/W1',1,8)")
    conn.execute("INSERT INTO enrich_status (isbn13) VALUES ('9788081281587')")
    conn.commit()
    gz = tmp_path / "hathi_full_test.txt.gz"
    with _gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write("htid\taccess\trights\tbib\tv\tinst\trecord\toclc\tisbn\n")
        fh.write("mdp.1\tdeny\tic\t1\tv.1\tMIU\t9\t123\t9788081281587\n")
    stats = m5.enrich_ht(conn, gz)
    assert stats["staged_rows"] >= 1
    row = conn.execute("SELECT ht_access FROM enrich_status "
                       "WHERE isbn13='9788081281587'").fetchone()
    assert row["ht_access"] == "deny"


def test_normalize_isbn_junk_unicode_digits():
    """Regression (2026-09-30): hathifile junk ISBN with Unicode subscript
    ('₂') passed .isdigit() guards then crashed int(). normalize_isbn must be
    total: junk -> None, never raise."""
    from lastcopy.isbn import normalize_isbn
    assert normalize_isbn("808128158\u2082") is None        # subscript junk
    assert normalize_isbn("\u2082" * 13) is None             # 13 unicode digits
    assert normalize_isbn("9" * 13) is None                  # bad checksum
    assert normalize_isbn("9788081281587") is not None       # valid still works


def test_ia_plan_and_hits_exclude_pallet_containers(tmp_path):
    """Regression (2026-09-30): unfiltered isbn: queries hit BWB donation
    pallet containers (bwb_daily_pallets_*, BWB-*) — inventory items with
    hundreds of ISBNs, NOT scans. Query must carry AND mediatype:texts and
    result recording must reject container identifiers."""
    import sqlite3
    from lastcopy import m5

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    m5.ensure_schema(conn)
    conn.executemany(
        "INSERT INTO enrich_workset (isbn13, work_key, edition_count, score) "
        "VALUES (?,?,1,8)",
        [(f"978000000000{i}", f"/works/W{i}") for i in range(4)])
    conn.executemany(
        "INSERT INTO enrich_status (isbn13) VALUES (?)",
        [(f"978000000000{i}",) for i in range(4)])
    conn.commit()

    plan = m5.build_ia_plan(conn, batch=4)
    row = conn.execute("SELECT query FROM ia_plan WHERE element_idx=0").fetchone()
    assert "AND mediatype:texts" in row["query"]
    assert row["query"].startswith("(") and row["query"].endswith(")")

    # stubbed response: one real scan + one pallet container
    class R:
        status_code = 200
        ok = True
        @staticmethod
        def json():
            return {"response": {"docs": [
                {"identifier": "realarchiveitem00book",
                 "isbn": ["9780000000000"]},
                {"identifier": "bwb_daily_pallets_2021-03-10",
                 "isbn": ["9780000000001", "9780000000002"]},
                {"identifier": "BWB-2024-08-28",
                 "isbn": ["9780000000003"]},
            ]}}
    stats = m5.execute_ia_element(conn, 0, get=lambda url, params: R(),
                                  sleep=lambda s: None)
    assert stats["hits"] == 1
    rows = conn.execute(
        "SELECT isbn13, ia_identifier FROM enrich_status "
        "WHERE ia_identifier IS NOT NULL").fetchall()
    assert len(rows) == 1 and rows[0]["isbn13"] == "9780000000000"
    assert rows[0]["ia_identifier"] == "realarchiveitem00book"
