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
    assert row["query"] == ("(" + " OR ".join(f"isbn:{v}" for v in UNRESOLVED)
                            + " AND mediatype:texts)")
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
    # Two-phase flow (2026-09-30 IA incident redesign): batch probe (rows=0,
    # numFound only) -> per-key single queries only for matching batches.
    # responses keyed by q:
    probe_q = conn.execute("SELECT query FROM ia_plan WHERE element_idx=0").fetchone()["query"]
    i2, i3, i7 = isbn13_for(2), isbn13_for(3), isbn13_for(7)
    by_q = {
        probe_q: FakeResponse(200, {"response": {"numFound": 2, "docs": []}}),
        f"(isbn:{i2}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "scan-of-2"}]}}),
        f"(isbn:{i3}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 0, "docs": []}}),
        f"(isbn:{i7}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "scan-of-both"}]}}),
    }
    http = FakeHTTP(by_q)
    out = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep)
    # element 0 = [i2,i3,i7]: probe + 3 singles; i2/i7 hit, i3 resolved no-hit
    assert out == {"element": 0, "skipped_done": False, "hits": 2}
    assert len(http.calls) == 4 and http.calls[0]["url"] == m5.IA_SEARCH_URL
    assert http.calls[0]["params"]["rows"] == 0        # probe is numFound-only
    assert all(http.calls[i]["params"]["rows"] == 1 for i in (1, 2, 3))
    assert all(s == 1.0 for s in http.sleeps)          # 1s rate discipline
    ia = dict(conn.execute("SELECT isbn13, ia_identifier FROM enrich_status"
                           ).fetchall())
    assert ia[i2] == "scan-of-2"
    assert ia[i7] == "scan-of-both"
    assert ia[isbn13_for(3)] is None         # unhit stays NULL
    assert ia[isbn13_for(5)] is None         # not in this element
    assert ia[isbn13_for(1)] is None         # already ht-allow -> never queried
    # plan row marked done; rerun skips it with zero HTTP
    out2 = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep)
    assert out2["skipped_done"] is True and out2["hits"] == 0
    assert len(http.calls) == 4
    # oclc element: probe hit -> per-oclc single resolves attribution
    m5.build_ia_plan(conn, batch=30)
    # unresolved now i3,i10,i11 -> oclcs 222(i3), 444(i10); element 1 = oclc pass
    oq = conn.execute("SELECT query FROM ia_plan WHERE element_idx=1").fetchone()["query"]
    http2 = FakeHTTP({
        oq: FakeResponse(200, {"response": {"numFound": 1, "docs": []}}),
        f"(oclc:222) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "oclc-scan"}]}}),
        f"(oclc:444) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 0, "docs": []}}),
    })
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


def test_ia_executor_probe_zero_resolves_batch_without_singles(m5_db):
    """Phase-A numFound=0 must resolve the whole batch with ONE request —
    the entire point of the two-phase design (IA omits doc isbn fields)."""
    conn = _post_ht_wd(m5_db)
    m5.build_ia_plan(conn, batch=3)
    probe_q = conn.execute("SELECT query FROM ia_plan WHERE element_idx=0").fetchone()["query"]
    http = FakeHTTP({probe_q: FakeResponse(
        200, {"response": {"numFound": 0, "docs": []}})})
    out = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep)
    assert out["hits"] == 0
    assert len(http.calls) == 1               # no per-key singles at all


# ------------------------------------------------------------------ stage 4
NT_BASIS = "full digital exists"
CR_BASIS = "edition_count=1; no digital in HT/IA/WD"
DD_BASIS = "not verified"

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
    ("dd_no_signals_no_oclc", 1, None, 0, None, None, True, "CR", CR_BASIS),  # checked+absent is CR not DD (2026-09-30 fix)
    ("dd_vu_shape", 4, None, 0, None, None, True, "VU", "edition_count=4"),  # checked+absent (2026-09-30 fix)
    ("not_dd_when_oclc", 1, None, 0, None, "666", True, "CR", CR_BASIS),
    ("dd_when_ia_unchecked", 1, None, None, None, None, False, "DD", DD_BASIS),  # unchecked is the only DD now
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
    """DD = only when the IA check never ran; any digital signal wins NT."""
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
    assert got[isbn13_for(1)] == "CR"   # checked + nothing anywhere -> CR
    assert got[isbn13_for(2)] == "CR"   # ht deny row present = partial, not DD
    assert got[isbn13_for(3)] == "NT"   # wd fulltext
    assert got[isbn13_for(4)] == "NT"   # ia hit
    assert got[isbn13_for(5)] == "CR"   # oclc present, still CR
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
                        "score,status,custody,custody_physical,holdings,"
                        "status_basis,gb_status")
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
    _isbns = ["9780000000101", "9780000000118", "9780000000125", "9780000000132"]
    conn.executemany(
        "INSERT INTO enrich_workset (isbn13, work_key, edition_count, score) "
        "VALUES (?,?,1,8)",
        [(v, f"/works/W{i}") for i, v in enumerate(_isbns)])
    conn.executemany(
        "INSERT INTO enrich_status (isbn13) VALUES (?)",
        [(v,) for v in _isbns])
    conn.commit()

    plan = m5.build_ia_plan(conn, batch=4)
    row = conn.execute("SELECT query FROM ia_plan WHERE element_idx=0").fetchone()
    assert "AND mediatype:texts" in row["query"]
    assert row["query"].startswith("(") and row["query"].endswith(")")

    # two-phase: probe hit -> per-ISBN singles; container ids rejected on resolve
    def r(ident=None, n=0):
        docs = [{"identifier": ident}] if ident else []
        return FakeResponse(200, {"response": {"numFound": n, "docs": docs}})
    by_q = {
        row["query"]: r(n=3),
        f"(isbn:9780000000101) AND mediatype:texts": r("realarchiveitem00book", 1),
        f"(isbn:9780000000118) AND mediatype:texts": r("bwb_daily_pallets_2021-03-10", 1),
        f"(isbn:9780000000125) AND mediatype:texts": r("BWB-2024-08-28", 1),
        f"(isbn:9780000000132) AND mediatype:texts": r(None, 0),
    }
    http = FakeHTTP(by_q)
    stats = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep)
    assert stats["hits"] == 1
    rows = conn.execute(
        "SELECT isbn13, ia_identifier FROM enrich_status "
        "WHERE ia_identifier IS NOT NULL").fetchall()
    assert len(rows) == 1 and rows[0]["isbn13"] == "9780000000101"
    assert rows[0]["ia_identifier"] == "realarchiveitem00book"


def test_dd_requires_unchecked_not_missing_oclc():
    """Regression (2026-09-30): 32,771 checked-and-absent books with no OCLC
    were parked in DD. No-OCLC is NOT un-verifiable once the IA ISBN pass
    covered the row; only genuinely unchecked rows are DD."""
    import sqlite3
    from lastcopy import m5

    class Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)
    # fully checked, no hits, no oclc, 1 edition -> CR (was DD before fix)
    r = {"ht_access": None, "wd_fulltext": 0, "ia_identifier": None,
         "sources_checked": "ht,wd,ia", "edition_count": 1, "oclc": None}
    assert m5._rule(r)[0] == "CR"
    # never IA-checked -> DD
    r2 = dict(r, sources_checked="ht,wd")
    assert m5._rule(r2)[0] == "DD"


# --------------------------------------------------- M5.5: ocaid-sweep
def _ed_ia(key: str, work: str, ia: str, field: str = "ocaid") -> str:
    """An edition record carrying a cross-edition IA link — the audit's
    false-CR shape: WORK's original edition has the free scan, the workset
    ISBN (reprint) does not. Real 2026 dump shape: `ocaid` (primary) or
    `ia_loaded_id`; there is NO `ia` field on edition records (2026-10-02)."""
    return dump_line("/type/edition", f"/books/{key}",
                     {"title": f"Ed {key}", "publish_date": "1930",
                      field: [ia], "works": [{"key": work}]})


def test_ocaid_sweep_cross_edition_upgrade(m5_db, tmp_path):
    conn, _ = m5_db
    m5.build_workset(conn)
    dump = write_gz(tmp_path / "editions_ocaid.txt.gz", EDITIONS + [
        _ed_ia("EX1", "/works/OW1", "scan123"),        # same work as isbn13_for(1)
        _ed_ia("EXZ", "/works/OWZ", "ignored456"),     # work outside the workset
        _ed_ia("EX2", "/works/OW1", "scan123b", field="ia_loaded_id"),  # fallback field
    ])
    stats = m5.ocaid_sweep(conn, dump, progress_every=0)
    assert stats["records_read"] == len(EDITIONS) + 3
    assert stats["works_with_ocaid"] == 1              # only OW1 staged
    assert stats["upgraded"] == 1
    row = conn.execute("SELECT ia_identifier, ia_source FROM enrich_status "
                       "WHERE isbn13=?", (isbn13_for(1),)).fetchone()
    assert row["ia_identifier"] == "scan123"
    assert row["ia_source"] == "ocaid"
    # works outside the set are ignored entirely
    assert conn.execute("SELECT COUNT(*) c FROM ocaid_stage "
                        "WHERE work_key='/works/OWZ'").fetchone()["c"] == 0
    # untouched rows keep NULL ia_source; assign-status derives NT from the
    # ocaid-sourced ia_identifier (no rule changes needed)
    n_null = conn.execute("SELECT COUNT(*) c FROM enrich_status "
                          "WHERE ia_source IS NULL").fetchone()["c"]
    assert n_null == 11
    m5.assign_status(conn)
    st = conn.execute("SELECT status, status_basis FROM enrich_status "
                      "WHERE isbn13=?", (isbn13_for(1),)).fetchone()
    assert st["status"] == "NT" and "ia hit" in st["status_basis"]
    # idempotent: rerunning the pass upgrades nothing new
    stats2 = m5.ocaid_sweep(conn, dump, progress_every=0)
    assert stats2["upgraded"] == 1


def test_ocaid_sweep_max_records_canary_cap(m5_db, tmp_path):
    conn, _ = m5_db
    m5.build_workset(conn)
    dump = write_gz(tmp_path / "editions_cap.txt.gz",
                    EDITIONS + [_ed_ia("EX1", "/works/OW1", "scan123")])
    stats = m5.ocaid_sweep(conn, dump, max_records=3, progress_every=0)
    assert stats["records_read"] == 3                 # cap honored
    assert stats["works_with_ocaid"] == 0 and stats["upgraded"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM ocaid_stage"
                        ).fetchone()["c"] == 0
    assert conn.execute("SELECT ia_identifier FROM enrich_status WHERE "
                        "isbn13=?", (isbn13_for(1),)).fetchone()[0] is None
    # full pass afterwards still lands the upgrade
    stats2 = m5.ocaid_sweep(conn, dump, progress_every=0)
    assert stats2["upgraded"] == 1


# --------------------------------------------------- M5.5: gb-trickle
def _gb_db(tmp_path, rows):
    """rows = [(isbn13, status, score)] -> workset + status rows."""
    conn = m4.connect(tmp_path / "gb.db")
    m5.ensure_schema(conn)
    for i, (i13, status, score) in enumerate(rows):
        conn.execute("INSERT INTO enrich_workset (isbn13, work_key, "
                     "edition_count, score, oclc, created_at) "
                     "VALUES (?,?,1,?,NULL,?)", (i13, f"/works/G{i}", score, m5.now()))
        conn.execute("INSERT INTO enrich_status (isbn13, status) "
                     "VALUES (?,?)", (i13, status))
    conn.commit()
    return conn


def _gb_resp(items):
    return FakeResponse(200, {"totalItems": len(items), "items": items})


def test_gb_trickle_statuses_budget_priority_resume(tmp_path, monkeypatch):
    from lastcopy.sources.gbooks import KEY_ENV

    monkeypatch.delenv(KEY_ENV, raising=False)
    key_file = tmp_path / "gbooks.key"
    key_file.write_text("TESTKEY", encoding="utf-8")
    i_cr_hi, i_cr_lo, i_nt, i_en = (isbn13_for(n) for n in (2, 1, 4, 3))
    conn = _gb_db(tmp_path, [(i_cr_hi, "CR", 9), (i_cr_lo, "CR", 5),
                             (i_nt, "NT", 99), (i_en, "EN", 7)])
    by_q = {
        i_cr_hi: _gb_resp([   # results, preview only -> metadata
            {"id": "volCRhi", "volumeInfo": {"title": "x"},
             "accessInfo": {"viewability": "PARTIAL"}}]),
        i_cr_lo: _gb_resp([]),                     # 0 results -> none
        i_nt: _gb_resp([   # full view -> full + identifier
            {"id": "volNT", "volumeInfo": {},
             "accessInfo": {"viewability": "FULL_PUBLIC_DOMAIN"}}]),
    }
    http = FakeHTTP(by_q)
    out = m5.gb_trickle(conn, 2, key_file=key_file, get=http.get,
                        sleep=http.sleep)
    # budget 2 with 4 eligible: CR-first then score DESC -> cr_hi, cr_lo only
    assert out["queried"] == 2
    assert out["counts"] == {"metadata": 1, "none": 1}
    assert len(http.calls) == 2
    assert all(c["url"] == m5.GB_VOLUMES_URL for c in http.calls)
    assert all(c["params"]["key"] == "TESTKEY" for c in http.calls)
    assert [c["params"]["q"] for c in http.calls] == \
        [i_cr_hi, i_cr_lo]
    assert all(s == 1.0 for s in http.sleeps)          # ~1 req/s discipline
    got = dict(conn.execute("SELECT isbn13, gb_status FROM enrich_status"
                            ).fetchall())
    assert got[i_cr_hi] == "metadata" and got[i_cr_lo] == "none"
    assert got[i_nt] is None and got[i_en] is None     # never reached
    ident = dict(conn.execute("SELECT isbn13, gb_identifier FROM enrich_status"
                              ).fetchall())
    assert ident[i_cr_hi] == "volCRhi" and ident[i_cr_lo] is None
    # resume: rerun skips set rows, finishes the rest
    by_q2 = {
        i_nt: _gb_resp([{"id": "volNT",
                                   "accessInfo": {"viewability":
                                                  "FULL_PUBLIC_DOMAIN"}}]),
        i_en: _gb_resp([]),
    }
    http2 = FakeHTTP(by_q2)
    out2 = m5.gb_trickle(conn, 10, key_file=key_file, get=http2.get,
                         sleep=http2.sleep)
    assert out2["queried"] == 2 and out2["counts"] == {"full": 1, "none": 1}
    assert i_cr_hi not in [c["params"]["q"] for c in http2.calls]
    got = dict(conn.execute("SELECT isbn13, gb_status FROM enrich_status"
                            ).fetchall())
    assert got[i_nt] == "full" and got[i_en] == "none"
    conn.close()


def test_gb_trickle_missing_key_raises(tmp_path, monkeypatch):
    from lastcopy.sources.gbooks import KeyRequiredError, KEY_ENV

    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setattr(m5, "GB_KEY_DEFAULT_FILES", ())  # skip server paths
    conn = _gb_db(tmp_path, [(isbn13_for(1), "CR", 5)])
    with pytest.raises(KeyRequiredError):
        m5.gb_trickle(conn, 1, key_file=tmp_path / "missing.key")


# ------------------------------------------- M5.5: export note + gb column
def test_export_workset_dynamic_provisional_note(m5_db, tmp_path):
    conn, _ = m5_db
    m5.build_workset(conn)
    # all rows gb-verified -> provisional clause dropped
    conn.execute("UPDATE enrich_status SET gb_status='none'")
    conn.commit()
    csv_path, md_path = tmp_path / "ws.csv", tmp_path / "ws.md"
    stats = m5.export_workset(conn, csv_path, md_path)
    assert stats["gb_pending"] is False
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith(",gb_status")
    assert lines[-1].startswith("978")               # data row, no note line
    assert "none" in lines[1].split(",")
    md = md_path.read_text(encoding="utf-8")
    assert "provisional" not in md and "gb_status" in md
    # any unverified row -> the provisional note returns
    conn.execute("UPDATE enrich_status SET gb_status=NULL "
                 "WHERE isbn13=?", (isbn13_for(1),))
    conn.commit()
    stats2 = m5.export_workset(conn, csv_path, md_path)
    assert stats2["gb_pending"] is True
    lines2 = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines2[-1] == f"# {m5.PROVISIONAL_NOTE}"
    assert m5.PROVISIONAL_NOTE in md_path.read_text(encoding="utf-8")


def test_cli_ocaid_sweep_and_gb_trickle_render(m5_db, tmp_path, capsys,
                                               monkeypatch):
    from lastcopy.sources.gbooks import KEY_ENV, KeyRequiredError

    conn, _ = m5_db
    m5.build_workset(conn)
    db = str(tmp_path / "m5.db")
    dump = write_gz(tmp_path / "editions_ocaid.txt.gz",
                    EDITIONS + [_ed_ia("EX1", "/works/OW1", "scan123")])
    assert cli_main(["--db", db, "ocaid-sweep", "--file", dump]) == 0
    out = capsys.readouterr().out
    assert "ocaid-sweep" in out and "upgraded" in out
    assert cli_main(["--db", db, "ocaid-sweep", "--file", dump,
                     "--max-records", "2"]) == 0
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(KeyRequiredError):
        cli_main(["--db", db, "gb-trickle", "--budget", "10",
                  "--key-file", str(tmp_path / "missing.key")])


# --------------------------------------------------- M5.6: custody
CUSTODY_CASES = [
    # (name, ht_access, ia_identifier, gb_status, ia_checked, expected)
    ("open_via_ht", "allow", None, None, True, "open"),
    ("open_via_ia", None, "some-scan", None, True, "open"),
    ("open_beats_restricted", "allow", "some-scan", "full", True, "open"),
    ("restricted_only_gb_full", None, None, "full", True, "restricted"),
    ("none_via_gb_metadata", None, None, "metadata", True, "none"),
    ("none_via_gb_none", None, None, "none", True, "none"),
    ("none_via_nothing", None, None, None, True, "none"),
    ("unknown_when_ia_unchecked", None, None, "full", False, "unknown"),
]


@pytest.mark.parametrize("name,ht,ia,gb,ia_checked,want",
                         CUSTODY_CASES, ids=[c[0] for c in CUSTODY_CASES])
def test_custody_rules_table_driven(tmp_path, name, ht, ia, gb, ia_checked,
                                    want):
    conn = m4.connect(tmp_path / "custody.db")
    m5.ensure_schema(conn)
    i13 = isbn13_for(1)
    conn.execute("INSERT INTO enrich_workset (isbn13, work_key, edition_count, "
                 "score, oclc, created_at) VALUES (?,?,?,?,?,?)",
                 (i13, "/works/X", 1, 5, None, m5.now()))
    conn.execute("INSERT INTO enrich_status (isbn13, ht_access, ia_identifier, "
                 "gb_status, sources_checked) VALUES (?,?,?,?,?)",
                 (i13, ht, ia, gb, "ht,wd" + (",ia" if ia_checked else "")))
    stats = m5.assign_custody(conn)
    row = conn.execute("SELECT custody FROM enrich_status").fetchone()
    assert row["custody"] == want
    assert stats["custody_assigned"] == 1
    assert stats["custody"] == {want: 1}
    # idempotent: recompute keeps the same tag
    m5.assign_custody(conn)
    assert conn.execute("SELECT custody FROM enrich_status"
                        ).fetchone()["custody"] == want
    conn.close()


def test_assign_status_chains_custody(tmp_path):
    conn = m4.connect(tmp_path / "chain.db")
    m5.ensure_schema(conn)
    conn.execute("INSERT INTO enrich_workset (isbn13, work_key, edition_count, "
                 "score, oclc, created_at) VALUES (?,?,?,?,?,?)",
                 (isbn13_for(1), "/works/X", 1, 5, None, m5.now()))
    conn.execute("INSERT INTO enrich_status (isbn13, ht_access, "
                 "sources_checked) VALUES (?, 'allow', 'ht,ia')",
                 (isbn13_for(1),))
    stats = m5.assign_status(conn)
    assert "custody" in stats and stats["custody"] == {"open": 1}
    assert conn.execute("SELECT custody FROM enrich_status"
                        ).fetchone()["custody"] == "open"
    conn.close()


def test_export_workset_custody_column(m5_db, tmp_path):
    conn = _post_ht_wd(m5_db)
    m5.assign_custody(conn)
    csv_path, md_path = tmp_path / "ws.csv", tmp_path / "ws.md"
    rc = cli_main(["--db", str(tmp_path / "m5.db"), "export-list", "--workset",
                   "--csv", str(csv_path), "--md", str(md_path)])
    assert rc == 0
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    hdr = lines[0].split(",")
    assert hdr.index("custody") == hdr.index("status") + 1   # after status
    data = [ln.split(",") for ln in lines[1:-1]]   # last line = note
    cust = {r[0]: r[hdr.index("custody")] for r in data}
    assert cust[isbn13_for(1)] == "open"          # ht allow
    assert cust[isbn13_for(11)] == "unknown"      # ia never ran for the row
    md = md_path.read_text(encoding="utf-8")
    assert "custody" in md


def test_cli_assign_custody_subcommand(m5_db, capsys):
    conn, tmp_path = m5_db
    m5.build_workset(conn)
    db = str(tmp_path / "m5.db")
    assert cli_main(["--db", db, "assign-custody"]) == 0
    out = capsys.readouterr().out
    assert "assign-custody" in out and "unknown=12" in out
    # recompute is idempotent
    assert cli_main(["--db", db, "assign-custody"]) == 0
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE custody='unknown'"
                        ).fetchone()["c"] == 12


# --------------------------------------------------- M5.8: parallel IA slices
def _tmp_of(m5_db):
    return m5_db[1]


def test_ia_execute_results_dir_writes_slice_done_no_db_writes(m5_db):
    """--results-dir mode: hits land in slice_<idx>.jsonl + done_<idx>
    marker; enrich_status / ia_plan / sources_checked all untouched."""
    conn = _post_ht_wd(m5_db)
    m5.build_ia_plan(conn, batch=3)
    probe_q = conn.execute(
        "SELECT query FROM ia_plan WHERE element_idx=0").fetchone()["query"]
    i2, i3, i7 = isbn13_for(2), isbn13_for(3), isbn13_for(7)
    by_q = {
        probe_q: FakeResponse(200, {"response": {"numFound": 2, "docs": []}}),
        f"(isbn:{i2}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "scan-of-2"}]}}),
        f"(isbn:{i3}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 0, "docs": []}}),
        f"(isbn:{i7}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "scan-of-7"}]}}),
    }
    http = FakeHTTP(by_q)
    rdir = _tmp_of(m5_db) / "results"
    out = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep,
                                results_dir=rdir)
    assert out == {"element": 0, "skipped_done": False, "hits": 2}
    slice_path = rdir / "slice_0.jsonl"
    lines = [json.loads(l) for l in
             slice_path.read_text(encoding="utf-8").splitlines()]
    assert lines == [{"isbn13": i2, "ia_identifier": "scan-of-2"},
                     {"isbn13": i7, "ia_identifier": "scan-of-7"}]
    assert (rdir / "done_0").exists()
    # NO DB writes: ia stays NULL, element not done, 'ia' never checked
    ia = dict(conn.execute("SELECT isbn13, ia_identifier FROM enrich_status"
                           ).fetchall())
    assert all(v is None for v in ia.values())
    assert conn.execute("SELECT done FROM ia_plan WHERE element_idx=0"
                        ).fetchone()["done"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%ia%'"
                        ).fetchone()["c"] == 0
    # rerun skips via the done marker: no HTTP, no file rewrite
    mtime = slice_path.stat().st_mtime_ns
    out2 = m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep,
                                 results_dir=rdir)
    assert out2["skipped_done"] is True and out2["hits"] == 0
    assert len(http.calls) == 4
    assert slice_path.stat().st_mtime_ns == mtime


def test_ia_execute_results_dir_works_on_read_only_db(m5_db):
    """Parallel executors open the DB mode=ro (connect_ro) — the executor
    must never need a write connection in results-dir mode."""
    conn = _post_ht_wd(m5_db)
    m5.build_ia_plan(conn, batch=3)
    probe_q = conn.execute(
        "SELECT query FROM ia_plan WHERE element_idx=0").fetchone()["query"]
    http = FakeHTTP({probe_q: FakeResponse(
        200, {"response": {"numFound": 0, "docs": []}})})
    ro = m5.connect_ro(_tmp_of(m5_db) / "m5.db")
    try:
        out = m5.execute_ia_element(ro, 0, get=http.get, sleep=http.sleep,
                                    results_dir=_tmp_of(m5_db) / "results-ro")
        assert out["hits"] == 0
        assert (_tmp_of(m5_db) / "results-ro" / "done_0").exists()
    finally:
        ro.close()


def test_merge_ia_results_applies_once_skips_unknown_and_set(m5_db, capsys):
    conn = _post_ht_wd(m5_db)
    m5.build_ia_plan(conn, batch=3)
    probe_q = conn.execute(
        "SELECT query FROM ia_plan WHERE element_idx=0").fetchone()["query"]
    i2, i3, i7 = isbn13_for(2), isbn13_for(3), isbn13_for(7)
    by_q = {
        probe_q: FakeResponse(200, {"response": {"numFound": 2, "docs": []}}),
        f"(isbn:{i2}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "scan-of-2"}]}}),
        f"(isbn:{i3}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 0, "docs": []}}),
        f"(isbn:{i7}) AND mediatype:texts":
            FakeResponse(200, {"response": {"numFound": 1, "docs": [
                {"identifier": "scan-of-7"}]}}),
    }
    http = FakeHTTP(by_q)
    rdir = _tmp_of(m5_db) / "results"
    m5.execute_ia_element(conn, 0, get=http.get, sleep=http.sleep,
                          results_dir=rdir)
    # a foreign slice with one unknown ISBN + one malformed line
    (rdir / "slice_99.jsonl").write_text(
        json.dumps({"isbn13": "9789999999993", "ia_identifier": "ghost"})
        + "\nnot-json\n", encoding="utf-8")
    db = str(_tmp_of(m5_db) / "m5.db")
    rc = cli_main(["--db", db, "merge-ia-results", "--results-dir", str(rdir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "merge-ia-results" in out and "applied=2" in out
    ia = dict(conn.execute("SELECT isbn13, ia_identifier FROM enrich_status"
                           ).fetchall())
    assert ia[i2] == "scan-of-2" and ia[i7] == "scan-of-7"
    # idempotent: second merge applies nothing, hits stay put
    rc2 = cli_main(["--db", db, "merge-ia-results", "--results-dir", str(rdir)])
    assert rc2 == 0
    assert "applied=0" in capsys.readouterr().out
    ia2 = dict(conn.execute("SELECT isbn13, ia_identifier FROM enrich_status"
                            ).fetchall())
    assert ia2 == ia
    # 'ia' checked recorded (assign-status needs it to avoid DD gaps)
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%ia%'"
                        ).fetchone()["c"] == 12


def test_cli_results_dir_requires_execute(m5_db, capsys):
    db = str(_tmp_of(m5_db) / "m5.db")
    rc = cli_main(["--db", db, "enrich-ia", "--results-dir", "/tmp/nope"])
    assert rc == 2
    assert "requires --execute" in capsys.readouterr().err


# ------------------------------------------- transport-error retry (2026-10-03)
def test_request_json_retries_transport_errors_then_succeeds():
    """httpx.ConnectError mid-run killed overnight jobs (2026-10-03 BnF
    postmortem): transport exceptions get the SAME exponential backoff as
    429/503. Two ConnectErrors then a 200 -> result returned, 2 backoffs."""
    import httpx

    state = {"n": 0}

    def get(url, params):
        state["n"] += 1
        if state["n"] <= 2:
            raise httpx.ConnectError("connection reset by peer")
        return FakeResponse(200, {"response": {"numFound": 0, "docs": []}})

    sleeps: list[float] = []
    data = m5._request_json(get, {"q": "x"}, sleeps.append, max_retries=3,
                            min_interval=0.5)
    assert data == {"response": {"numFound": 0, "docs": []}}
    assert state["n"] == 3                      # 2 raises + 1 success
    # 3 min-interval sleeps + backoff 2^0 + 2^1
    assert sleeps == [0.5, 1.0, 0.5, 2.0, 0.5]


def test_request_json_transport_error_exhaustion_returns_none():
    """Always-raising get exhausts retries -> None (failed row, retried
    next run); never crashes the caller."""
    import httpx

    calls = {"n": 0}

    def get(url, params):
        calls["n"] += 1
        raise httpx.ConnectError("connection reset by peer")

    sleeps: list[float] = []
    data = m5._request_json(get, {"q": "x"}, sleeps.append, max_retries=3,
                            min_interval=0.5)
    assert data is None
    assert calls["n"] == 3                      # max_retries attempts, no more
    assert sleeps == [0.5, 1.0, 0.5, 2.0, 0.5]  # final attempt: no backoff after


# ------------------------------------------- M5.9: physical custody
def _phys_db(tmp_path, rows):
    """rows = [(isbn13, status, [(institution, record_id), ...])] ->
    workset + status rows + holdings rows."""
    conn = m4.connect(tmp_path / "phys.db")
    m5.ensure_schema(conn)
    for i, (i13, status, holds) in enumerate(rows):
        conn.execute("INSERT INTO enrich_workset (isbn13, work_key, "
                     "edition_count, score, oclc, created_at) "
                     "VALUES (?,?,1,?,NULL,?)", (i13, f"/works/P{i}", 5,
                                                 m5.now()))
        conn.execute("INSERT INTO enrich_status (isbn13, status) "
                     "VALUES (?,?)", (i13, status))
        for inst, rid in holds:
            conn.execute("INSERT OR IGNORE INTO holdings "
                         "(isbn13, institution, record_id) VALUES (?,?,?)",
                         (i13, inst, rid))
    conn.commit()
    return conn


def test_custody_physical_wild_single_multi(tmp_path):
    conn = _phys_db(tmp_path, [
        (isbn13_for(1), "CR", []),                          # wild
        (isbn13_for(2), "CR", [("dnb", "d1")]),             # single
        (isbn13_for(3), "CR", [("dnb", "d2"), ("loc", "l2")]),  # multi (dnb+loc)
        (isbn13_for(4), "EN", [("bnf", "b4")]),             # single, non-CR
    ])
    stats = m5.assign_holdings_summary(conn)
    got = dict(conn.execute("SELECT isbn13, custody_physical FROM enrich_status"
                            ).fetchall())
    assert got[isbn13_for(1)] == "wild"
    assert got[isbn13_for(2)] == "single"
    assert got[isbn13_for(3)] == "multi"
    assert got[isbn13_for(4)] == "single"
    assert stats["custody_physical"] == {"wild": 1, "single": 2, "multi": 1}
    assert dict(conn.execute("SELECT isbn13, holdings FROM enrich_status"
                             ).fetchall())[isbn13_for(3)] == "dnb,loc"
    conn.close()


def test_custody_physical_unheld_nt_and_idempotency(tmp_path):
    conn = _phys_db(tmp_path, [
        (isbn13_for(1), "NT", []),               # digital exists, no holdings
        (isbn13_for(2), "NT", [("dnb", "d2")]),  # NT + holdings -> single
    ])
    m5.assign_holdings_summary(conn)
    got = dict(conn.execute("SELECT isbn13, custody_physical FROM enrich_status"
                            ).fetchall())
    assert got[isbn13_for(1)] == "unheld-nt"
    assert got[isbn13_for(2)] == "single"
    # idempotent: rerun keeps identical tags
    m5.assign_holdings_summary(conn)
    got2 = dict(conn.execute("SELECT isbn13, custody_physical FROM enrich_status"
                             ).fetchall())
    assert got2 == got
    conn.close()


def test_custody_report_buckets(tmp_path):
    conn = _phys_db(tmp_path, [
        (isbn13_for(1), "CR", [("dnb", "d1")]),             # safe (CR+single)
        (isbn13_for(2), "EN", [("dnb", "d2")]),             # single but not CR
        (isbn13_for(3), "CR", [("dnb", "d3"), ("loc", "l3")]),  # captive-secure
        (isbn13_for(4), "CR", []),                          # wild
        (isbn13_for(5), "NT", []),                          # none of the buckets
    ])
    # restricted custody (gb-only full) is safe-but-not-really-safe regardless
    conn.execute("UPDATE enrich_status SET custody='restricted' "
                 "WHERE isbn13=?", (isbn13_for(2),))
    conn.commit()
    m5.assign_holdings_summary(conn)
    buckets = m5.custody_report(conn)
    assert buckets == {"safe-but-not-really-safe": 2,   # i1 (CR+single), i2 (restricted)
                       "wild": 1,                       # i4
                       "captive-secure": 1}             # i3
    conn.close()


def test_custody_report_cli_and_export_column(tmp_path, capsys):
    conn = _phys_db(tmp_path, [
        (isbn13_for(1), "CR", [("dnb", "d1")]),
        (isbn13_for(2), "CR", []),
    ])
    db = str(tmp_path / "phys.db")
    assert cli_main(["--db", db, "assign-holdings-summary"]) == 0
    assert cli_main(["--db", db, "custody-report"]) == 0
    out = capsys.readouterr().out
    assert "custody-report" in out
    assert "safe-but-not-really-safe=1" in out and "wild=1" in out
    assert cli_main(["--db", db, "export-list", "--workset",
                     "--csv", str(tmp_path / "ws.csv")]) == 0
    lines = (tmp_path / "ws.csv").read_text(encoding="utf-8").splitlines()
    hdr = lines[0].split(",")
    assert hdr.index("custody_physical") == hdr.index("custody") + 1
    phys = {ln.split(",")[0]: ln.split(",")[hdr.index("custody_physical")]
            for ln in lines[1:] if ln.startswith("978")}
    assert phys[isbn13_for(1)] == "single"
    assert phys[isbn13_for(2)] == "wild"
    conn.close()
