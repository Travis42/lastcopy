"""M4 — THE LIST: offline candidate generator tests (SPEC M4 item 5).

Gzipped fixture mini-dumps in the REAL OL dump TSV format
(type\tkey\trevision\tlast_modified\tJSON), including edge records:
no-ia, multi-ISBN, isbn10-only, missing publish_date, unicode titles.
Plus: counting correctness (1-vs-5 edition works), authors join, scoring
determinism, golden e2e list, bounded-memory assertion on a 50k fixture,
and the documented stream restart-on-failure path. No network access.
"""

from __future__ import annotations

import gzip
import json
import resource
import subprocess
import sys

import pytest

from lastcopy import m4
from lastcopy.cli import main as cli_main
from lastcopy.isbn import isbn10_to_13, isbn13_checksum_ok

TS = "2009-03-27T12:00:00.000000+00:00"


# ------------------------------------------------------------ fixture builders
def isbn13_for(n: int, prefix: str = "9780") -> str:
    """Valid ISBN-13 derived deterministically from an int."""
    core = prefix + f"{n:08d}"
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def isbn10_for(n: int) -> str:
    """Valid ISBN-10 derived deterministically from an int."""
    core = f"0{n:08d}"
    total = sum(int(c) * (10 - i) for i, c in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


def dump_line(type_: str, key: str, obj: dict) -> str:
    return "\t".join([type_, key, "1", TS,
                      json.dumps(obj, ensure_ascii=False)])


def write_gz(path, lines) -> str:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


BAD_ISBN13 = "9780000000000"  # checksum-invalid


def edge_editions_lines() -> list[str]:
    return [
        # no-ia record, valid isbn13, pre-1927 non-eng -> top candidate
        # (REAL dump shape: language as {"key": "/languages/por"})
        dump_line("/type/edition", "/books/OL1M", {
            "title": "Poemas Açorianos", "publish_date": "1920",
            "languages": [{"key": "/languages/por"}], "isbn_13": [isbn13_for(1)],
            "publishers": ["Officina de Artes"], "oclc_numbers": ["123"],
            "works": [{"key": "/works/OLW1"}]}),
        # IA scan present -> must be excluded from candidates
        dump_line("/type/edition", "/books/OL2M", {
            "title": "Popular Scanned", "publish_date": "1975",
            "languages": [{"key": "/languages/eng"}], "isbn_13": [isbn13_for(2)],
            "ia": ["popularscanned00ol"], "works": [{"key": "/works/OLW2"}]}),
        # multi-isbn: two valid isbn13 -> two rows, same edition data
        dump_line("/type/edition", "/books/OL3M", {
            "title": "Dual ISBN Edition", "publish_date": "1950",
            "isbn_13": [isbn13_for(3), isbn13_for(4)],
            "works": [{"key": "/works/OLW3"}]}),
        # isbn10-only -> kept via conversion
        dump_line("/type/edition", "/books/OL4M", {
            "title": "Old Ten", "publish_date": "March 1962",
            "isbn_10": [isbn10_for(5)], "languages": ["/languages/eng"],  # string back-compat
            "works": [{"key": "/works/OLW4"}]}),
        # missing publish_date -> year NULL (era unknown)
        dump_line("/type/edition", "/books/OL5M", {
            "title": "No Date", "isbn_13": [isbn13_for(6)],
            "languages": [{"key": "/languages/deu"}], "works": [{"key": "/works/OLW5"}]}),
        # invalid-checksum isbn only -> dropped entirely
        dump_line("/type/edition", "/books/OL6M", {
            "title": "Broken ISBN", "isbn_13": [BAD_ISBN13],
            "works": [{"key": "/works/OLW6"}]}),
        # unicode + no language + modern -> unknown-language boost only
        dump_line("/type/edition", "/books/OL7M", {
            "title": "Ilhas — Maré de Histórias 📚", "publish_date": "1999-01-01",
            "isbn_13": [isbn13_for(7)], "works": [{"key": "/works/OLW7"}]}),
        # malformed JSON line -> counted bad, not fatal
        "/type/edition\t/books/OL8M\t1\t" + TS + "\t{not json",
        # non-edition line type -> skipped
        dump_line("/type/permission", "/books/OL9M", {"whatever": 1}),
    ]


def works_lines() -> list[str]:
    return [
        dump_line("/type/work", f"/works/OLW{i}", {
            "title": f"Work {i}",
            "authors": ([{"key": f"/authors/OLA{i}"}] if i not in (3, 7) else
                        ([{"key": "/authors/OLA3a"}, {"key": "/authors/OLA3b"}]
                         if i == 3 else [{"key": "/authors/OLA7"}])),
        }) for i in range(1, 8)
    ]


def authors_lines() -> list[str]:
    names = {1: "Maria Fonseca", 2: "John Smith", 3: None, 4: "A. B. Brown",
             5: "K. Müller", 6: "Nobody", 7: None}
    out = []
    for i in range(1, 8):
        if i == 3:
            out.append(dump_line("/type/author", "/authors/OLA3a", {"name": "Ana Silva"}))
            out.append(dump_line("/type/author", "/authors/OLA3b", {"name": "Bruno Costa"}))
        elif i == 7:
            out.append(dump_line("/type/author", "/authors/OLA7",
                                 {"personal_name": "nameless key-only"}))
        else:
            out.append(dump_line("/type/author", f"/authors/OLA{i}",
                                 {"name": names[i]}))
    return out


@pytest.fixture()
def mini_db(tmp_path):
    write_gz(tmp_path / "works.txt.gz", works_lines())
    write_gz(tmp_path / "authors.txt.gz", authors_lines())
    write_gz(tmp_path / "editions.txt.gz", edge_editions_lines())
    conn = m4.connect(tmp_path / "m4.db")
    yield conn, tmp_path
    conn.close()


# ---------------------------------------------------------------- ingest-works
def test_ingest_works_authors_join(mini_db):
    conn, tmp_path = mini_db
    stats = m4.ingest_works(conn, tmp_path / "works.txt.gz",
                            tmp_path / "authors.txt.gz", progress_every=0)
    assert stats["works"] == 7 and stats["authors"] == 7  # OLA7 is nameless -> row kept, not counted
    w3 = conn.execute("SELECT author_keys FROM works_ref WHERE work_key='/works/OLW3'"
                      ).fetchone()
    assert json.loads(w3["author_keys"]) == ["/authors/OLA3a", "/authors/OLA3b"]
    # nameless author skipped at ingest; export falls back to the raw key
    assert conn.execute("SELECT COUNT(*) c FROM authors_ref WHERE name IS NULL"
                        ).fetchone()["c"] == 0


# --------------------------------------------- real-dump regression (SPEC-M4-PARSEFIX)
def test_real_dump_language_dict_shape(tmp_path):
    """Real dumps: languages [{"key": "/languages/eng"}] -> "eng" (2026-09-30
    incident: str(dict) leaked "eng'}" into editions_ref and distorted ranking)."""
    lines = [
        dump_line("/type/edition", "/books/DL1M", {
            "title": "Dict Eng", "publish_date": "1930",
            "languages": [{"key": "/languages/eng"}], "isbn_13": [isbn13_for(41)],
            "works": [{"key": "/works/OLD1"}]}),
        dump_line("/type/edition", "/books/DL2M", {
            "title": "Dict Other", "publish_date": "1931",
            "languages": [{"key": "/languages/por"}], "isbn_13": [isbn13_for(42)],
            "works": [{"key": "/works/OLD2"}]}),
        dump_line("/type/edition", "/books/DL3M", {  # string back-compat
            "title": "String Lang", "publish_date": "1932",
            "languages": ["/languages/deu"], "isbn_13": [isbn13_for(43)],
            "works": [{"key": "/works/OLD3"}]}),
        dump_line("/type/edition", "/books/DL4M", {  # no languages -> NULL
            "title": "No Lang", "isbn_13": [isbn13_for(44)],
            "works": [{"key": "/works/OLD4"}]}),
    ]
    conn = m4.connect(tmp_path / "d.db")
    write_gz(tmp_path / "e.txt.gz", lines)
    m4.ingest_editions(conn, file=tmp_path / "e.txt.gz", progress_every=0)
    langs = dict(conn.execute("SELECT isbn13, language FROM editions_ref"
                              ).fetchall())
    assert langs[isbn13_for(41)] == "eng"
    assert langs[isbn13_for(42)] == "por"
    assert langs[isbn13_for(43)] == "deu"
    assert langs[isbn13_for(44)] is None
    # zero dict-repr artifacts anywhere in stored rows
    assert conn.execute("SELECT COUNT(*) c FROM editions_ref "
                        "WHERE language LIKE '%}%'").fetchone()["c"] == 0
    conn.close()


def test_real_dump_works_author_dict_shape(tmp_path):
    """Real dumps: works authors as dicts must yield /authors/OL…A keys that
    resolve to names (2026-09-30 incident: author_keys collapsed to '[]')."""
    works = [
        dump_line("/type/work", "/works/OLWA", {
            "title": "One Author",
            "authors": [{"key": "/authors/OL123A"}]}),
        dump_line("/type/work", "/works/OLWB", {
            "title": "Two Authors",
            "authors": [{"author": {"key": "/authors/OL124A"}},
                        {"key": "/authors/OL125A"}]}),
        dump_line("/type/work", "/works/OLWC", {"title": "No Authors"}),
    ]
    authors = [
        dump_line("/type/author", "/authors/OL123A", {"name": "Real One"}),
        dump_line("/type/author", "/authors/OL124A", {"name": "Real Two"}),
        dump_line("/type/author", "/authors/OL125A", {"name": "Real Three"}),
    ]
    write_gz(tmp_path / "w.txt.gz", works)
    write_gz(tmp_path / "a.txt.gz", authors)
    conn = m4.connect(tmp_path / "d.db")
    m4.ingest_works(conn, tmp_path / "w.txt.gz", tmp_path / "a.txt.gz",
                    progress_every=0)
    keys = dict(conn.execute("SELECT work_key, author_keys FROM works_ref"
                             ).fetchall())
    assert json.loads(keys["/works/OLWA"]) == ["/authors/OL123A"]
    assert json.loads(keys["/works/OLWB"]) == ["/authors/OL124A",
                                               "/authors/OL125A"]
    assert json.loads(keys["/works/OLWC"]) == []
    assert m4.resolve_authors(conn, keys["/works/OLWA"]) == "Real One"
    assert m4.resolve_authors(conn, keys["/works/OLWB"]) == \
        "Real Two; Real Three"
    assert m4.resolve_authors(conn, keys["/works/OLWC"]) == ""
    conn.close()


# ------------------------------------------------------------- ingest-editions
def test_editions_ref_schema_is_slim(mini_db):
    """Slim storage (Theory 2026-09-30): only the columns downstream uses."""
    conn, _ = mini_db
    cols = [r["name"] for r in conn.execute(
        "PRAGMA table_info(editions_ref)")]
    assert cols == ["isbn13", "edition_key", "work_key", "year", "language", "ia"]
    for dropped in ("isbn10", "title", "publishers", "oclc_numbers"):
        assert dropped not in cols


def test_ingest_editions_edges(mini_db):
    conn, tmp_path = mini_db
    stats = m4.ingest_editions(conn, file=tmp_path / "editions.txt.gz",
                               progress_every=0)
    # OL1,2,3(x2),4,5,7 kept; OL6 invalid-isbn dropped; OL8 malformed; OL9 non-book
    assert stats["isbn_keyed_rows"] == 7 and stats["restarts"] == 0
    assert isbn13_checksum_ok(isbn13_for(1))
    r1 = conn.execute("SELECT * FROM editions_ref WHERE isbn13=?", (isbn13_for(1),)
                      ).fetchone()
    assert r1["year"] == 1920 and r1["language"] == "por" and r1["ia"] is None
    assert r1["work_key"] == "/works/OLW1" and r1["edition_key"] == "/books/OL1M"
    # isbn10-only converted to isbn13 (isbn10 itself no longer stored)
    r4 = conn.execute("SELECT * FROM editions_ref WHERE isbn13=?",
                      (isbn10_to_13(isbn10_for(5)),)).fetchone()
    assert r4["year"] == 1962 and r4["isbn13"] == isbn10_to_13(isbn10_for(5))
    # multi-isbn -> both rows share edition data
    for i in (3, 4):
        row = conn.execute("SELECT * FROM editions_ref WHERE isbn13=?",
                           (isbn13_for(i),)).fetchone()
        assert row["work_key"] == "/works/OLW3" and row["year"] == 1950
    # missing publish_date -> NULL year
    r5 = conn.execute("SELECT year FROM editions_ref WHERE isbn13=?",
                      (isbn13_for(6),)).fetchone()
    assert r5["year"] is None
    # broken-checksum ISBN never stored
    assert conn.execute("SELECT COUNT(*) c FROM editions_ref WHERE isbn13=?",
                        (BAD_ISBN13,)).fetchone()["c"] == 0


def test_parse_year_lenient():
    assert m4.parse_year("1920") == 1920
    assert m4.parse_year("March 1962") == 1962
    assert m4.parse_year("1999-01-01") == 1999
    assert m4.parse_year("circa 2202") is None   # outside sanity window
    assert m4.parse_year(None) is None
    assert m4.parse_year("no digits") is None


def test_counting_one_vs_five(mini_db):
    """edition_count backfilled in SQLite: 1-edition vs 5-edition works."""
    conn, tmp_path = mini_db
    lines = [
        dump_line("/type/edition", "/books/OLA1M", {
            "title": "Lone", "isbn_13": [isbn13_for(11)],
            "works": [{"key": "/works/OLONE"}]}),
    ] + [
        dump_line("/type/edition", f"/books/OLB{i}M", {
            "title": f"Many {i}", "isbn_13": [isbn13_for(20 + i)],
            "works": [{"key": "/works/OLMANY"}]}) for i in range(5)
    ]
    write_gz(tmp_path / "counts.txt.gz", lines)
    wl = [dump_line("/type/work", k, {"title": t})
          for k, t in [("/works/OLONE", "Lone"), ("/works/OLMANY", "Many")]]
    write_gz(tmp_path / "w.txt.gz", wl)
    m4.ingest_works(conn, tmp_path / "w.txt.gz", tmp_path / "w.txt.gz",
                    progress_every=0)
    m4.ingest_editions(conn, file=tmp_path / "counts.txt.gz", progress_every=0)
    counts = dict(conn.execute("SELECT work_key, edition_count FROM works_ref"
                               ).fetchall())
    assert counts == {"/works/OLONE": 1, "/works/OLMANY": 5}


def test_stream_restart_from_zero(mini_db, capsys):
    """Mid-stream failure -> restart from zero; idempotent upserts, no dupes."""
    conn, tmp_path = mini_db
    good_path = tmp_path / "editions.txt.gz"

    class FlakyOpener:
        calls = 0

        def __call__(self, path=None, url=None):
            FlakyOpener.calls += 1
            if FlakyOpener.calls == 1:
                raise m4.StreamFailed("simulated dropped connection")
            return m4.open_dump(path=path, url=url)

    stats = m4.ingest_editions(conn, file=good_path, progress_every=0,
                               retries=3, _opener=FlakyOpener())
    assert stats["restarts"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM editions_ref").fetchone()["c"] == 7
    assert "restarting from zero" in capsys.readouterr().err


def test_ingest_editions_retries_exhausted(mini_db):
    conn, tmp_path = mini_db

    def always_dead(path=None, url=None):
        raise m4.StreamFailed("curl exited 56")

    with pytest.raises(m4.StreamFailed):
        m4.ingest_editions(conn, file=tmp_path / "editions.txt.gz",
                           progress_every=0, retries=2, _opener=always_dead)


def test_progress_log_interval(mini_db, capsys):
    conn, tmp_path = mini_db
    m4.ingest_editions(conn, file=tmp_path / "editions.txt.gz", progress_every=3)
    err = capsys.readouterr().err
    assert "[editions] 3 records read" in err and "[editions] done:" in err


# ---------------------------------------------------------------- scoring
def test_scoring_deterministic_and_weighted():
    f = m4.score_candidate
    assert f(1, "por", 1910) == (8, "editions=1(+3); lang=por(+2); era=pre-1927(+3)")
    assert f(1, "eng", 1910) == (6, "editions=1(+3); lang=eng(+0); era=pre-1927(+3)")
    assert f(1, "eng", 1950) == (5, "editions=1(+3); lang=eng(+0); era=1927-1969(+2)")
    assert f(1, "eng", 1980) == (3, "editions=1(+3); lang=eng(+0); era=1970+(+0)")
    assert f(2, "por", 1930) == (6, "editions=2(+2); lang=por(+2); era=1927-1969(+2)")
    assert f(3, "eng", 1990) == (1, "editions=3(+1); lang=eng(+0); era=1970+(+0)")
    # determinism: same inputs, same outputs, always
    for args in [(1, "por", 1910), (3, None, None), (2, "eng", 1969)]:
        assert f(*args) == f(*args)


# ---------------------------------------------------------------- gen + export
def _full_pipeline(conn, tmp_path):
    m4.ingest_works(conn, tmp_path / "works.txt.gz", tmp_path / "authors.txt.gz",
                    progress_every=0)
    m4.ingest_editions(conn, file=tmp_path / "editions.txt.gz", progress_every=0)
    return m4.gen_candidates(conn, max_editions=1)


def test_gen_candidates_join_and_filters(mini_db):
    conn, tmp_path = mini_db
    stats = _full_pipeline(conn, tmp_path)
    # IA-present row excluded; every surviving edge work has edition_count 1
    assert stats["candidates"] == 6  # OLW1,3,3(dup isbn),4,5,7
    isbns = {r["isbn13"] for r in conn.execute("SELECT isbn13 FROM candidates")}
    assert isbn13_for(2) not in isbns
    assert isbn13_for(1) in isbns and isbn10_to_13(isbn10_for(5)) in isbns
    # lang filter
    stats = m4.gen_candidates(conn, max_editions=1, lang="por")
    assert stats["candidates"] == 1
    # year window filter
    stats = m4.gen_candidates(conn, max_editions=1, from_year=1900, to_year=1969)
    isbns = {r["isbn13"] for r in conn.execute("SELECT isbn13 FROM candidates")}
    assert isbns == {isbn13_for(1), isbn13_for(3), isbn13_for(4),
                     isbn10_to_13(isbn10_for(5))}
    # max-editions cap: raise to 5 -> still 6 (fixture works all have 1 edition
    # except OLW2/OLW3 which hold IA/multi rows; none has >1 per work)


def test_gen_candidates_determinism(mini_db):
    conn, tmp_path = mini_db
    _full_pipeline(conn, tmp_path)
    snap1 = conn.execute("SELECT * FROM candidates ORDER BY isbn13").fetchall()
    m4.gen_candidates(conn, max_editions=1)  # regenerate from same store
    snap2 = conn.execute("SELECT * FROM candidates ORDER BY isbn13").fetchall()
    assert [tuple(r) for r in snap1] == [tuple(r) for r in snap2]


GOLDEN_CSV = """isbn13,title,author,year,language,edition_count,score,rationale
{i1},Poemas Açorianos,Maria Fonseca,1920,por,1,8,editions=1(+3); lang=por(+2); era=pre-1927(+3)
{i3},Dual ISBN Edition,Ana Silva; Bruno Costa,1950,,1,6,editions=1(+3); lang=unknown(+1); era=1927-1969(+2)
{i4},Dual ISBN Edition,Ana Silva; Bruno Costa,1950,,1,6,editions=1(+3); lang=unknown(+1); era=1927-1969(+2)
{i5},Old Ten,A. B. Brown,1962,eng,1,5,editions=1(+3); lang=eng(+0); era=1927-1969(+2)
{i6},No Date,K. Müller,,deu,1,5,editions=1(+3); lang=deu(+2); era=unknown(+0)
{i7},Ilhas — Maré de Histórias 📚,OLA7,1999,,1,4,editions=1(+3); lang=unknown(+1); era=1970+(+0)
""".format(i1=isbn13_for(1), i3=isbn13_for(3), i4=isbn13_for(4),
           i5=isbn10_to_13(isbn10_for(5)), i6=isbn13_for(6), i7=isbn13_for(7))


def test_golden_e2e_list(mini_db, tmp_path):
    conn, _ = mini_db
    _full_pipeline(conn, tmp_path)
    out = tmp_path / "list.csv"
    stats = m4.export_list(conn, top=100, csv_path=out, md_path=tmp_path / "list.md",
                           editions_dump=tmp_path / "editions.txt.gz")
    assert stats["titles_backfilled"] == 6
    assert out.read_text(encoding="utf-8") == GOLDEN_CSV
    md = (tmp_path / "list.md").read_text(encoding="utf-8")
    assert "| 1 | " + isbn13_for(1) + " | Poemas Açorianos | Maria Fonseca" in md
    assert out.read_text(encoding="utf-8").splitlines()[0] == \
        "isbn13,title,author,year,language,edition_count,score,rationale"


def test_export_titles_null_without_dump(mini_db, tmp_path):
    """No editions_dump -> candidates.title NULL, CSV emits empty, no crash."""
    conn, _ = mini_db
    _full_pipeline(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) c FROM candidates "
                        "WHERE title IS NULL").fetchone()["c"] == 6
    out = tmp_path / "null.csv"
    stats = m4.export_list(conn, top=100, csv_path=out)
    assert stats["titles_backfilled"] == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[1] == f"{isbn13_for(1)},,Maria Fonseca,1920,por,1,8," \
                       "editions=1(+3); lang=por(+2); era=pre-1927(+3)"


# ------------------------------------------------------------- title backfill
def test_backfill_multi_isbn_and_precedence(mini_db, tmp_path):
    """One record fans out to every winner ISBN; title->full_title->subtitle."""
    conn, _ = mini_db
    _full_pipeline(conn, tmp_path)
    # precedence: full_title only, subtitle only, title beats both
    dump = tmp_path / "titles.txt.gz"
    write_gz(dump, [
        dump_line("/type/edition", "/books/TA", {
            "full_title": "Full Only", "isbn_13": [isbn13_for(1)],
            "works": [{"key": "/works/OLW1"}]}),
        dump_line("/type/edition", "/books/TB", {
            "subtitle": "Sub Only", "isbn_13": [isbn13_for(6)],
            "works": [{"key": "/works/OLW5"}]}),
        dump_line("/type/edition", "/books/TC", {
            "title": "The Title", "full_title": "Shadowed", "subtitle": "Also",
            "isbn_13": [isbn13_for(3), isbn13_for(4)],
            "works": [{"key": "/works/OLW3"}]}),
        # isbn10-only winner matched via conversion
        dump_line("/type/edition", "/books/TD", {
            "title": "Old Ten", "isbn_10": [isbn10_for(5)],
            "works": [{"key": "/works/OLW4"}]}),
    ])
    out = tmp_path / "t.csv"
    stats = m4.export_list(conn, top=100, csv_path=out, editions_dump=dump)
    assert stats["titles_backfilled"] == 5  # 1 + 1 + 2 (multi-isbn) + 1
    titles = dict(conn.execute("SELECT isbn13, title FROM candidates"
                               ).fetchall())
    assert titles[isbn13_for(1)] == "Full Only"
    assert titles[isbn13_for(6)] == "Sub Only"
    assert titles[isbn13_for(3)] == "The Title"
    assert titles[isbn13_for(4)] == "The Title"   # multi-ISBN record fills both
    assert titles[isbn10_to_13(isbn10_for(5))] == "Old Ten"


def test_backfill_first_record_wins_and_absent_stays_null(mini_db, tmp_path):
    conn, _ = mini_db
    _full_pipeline(conn, tmp_path)
    dump = tmp_path / "dup.txt.gz"
    write_gz(dump, [
        dump_line("/type/edition", "/books/X1", {
            "title": "First Wins", "isbn_13": [isbn13_for(1)],
            "works": [{"key": "/works/OLW1"}]}),
        dump_line("/type/edition", "/books/X2", {
            "title": "Second Loses", "isbn_13": [isbn13_for(1)],
            "works": [{"key": "/works/OLW1"}]}),
    ])
    out = tmp_path / "d.csv"
    m4.export_list(conn, top=100, csv_path=out, editions_dump=dump)
    title = conn.execute("SELECT title FROM candidates WHERE isbn13=?",
                         (isbn13_for(1),)).fetchone()["title"]
    assert title == "First Wins"
    # winner absent from the dump -> title stays NULL, no crash
    assert conn.execute("SELECT title FROM candidates WHERE isbn13=?",
                        (isbn13_for(7),)).fetchone()["title"] is None


def test_export_list_feeds_registry_ingest(mini_db, tmp_path):
    """The M4 CSV must slide into the existing registry ingest unchanged."""
    conn, _ = mini_db
    _full_pipeline(conn, tmp_path)
    out = tmp_path / "list.csv"
    m4.export_list(conn, top=3, csv_path=out)
    rc = cli_main(["--db", str(tmp_path / "reg.db"), "ingest", "--csv", str(out)])
    assert rc == 0
    import sqlite3
    reg = sqlite3.connect(tmp_path / "reg.db")
    rows = reg.execute("SELECT work_key, title, author, year, language "
                       "FROM editions ORDER BY work_key").fetchall()
    assert len(rows) == 3
    assert all(r[0].startswith("978") for r in rows)  # ISBN-keyed, not bib-stub
    reg.close()


# ---------------------------------------------------------------- bounded memory
MEM_BUDGET_KB = 512 * 1024  # test asserts well under the 1.5G hard ceiling


def _big_dump(path, n: int) -> str:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for i in range(n):
            obj = {"title": f"Book {i} — título com acentos",
                   "publish_date": f"{1900 + i % 130}",
                   "isbn_13": [isbn13_for(i % 90_000_000)],
                   "languages": ["/languages/por"],
                   "publishers": ["Ed"], "works": [{"key": f"/works/OLW{i}"}],
                   "notes": "x" * 200}
            f.write(dump_line("/type/edition", f"/books/OL{i}M", obj) + "\n")
    return str(path)


def test_ingest_editions_bounded_memory_50k(tmp_path):
    """SPEC M4 item 5: 50k-record fixture; fail if peak RSS > budget."""
    dump = _big_dump(tmp_path / "big.txt.gz", 50_000)
    script = (f"from lastcopy import m4; "
              f"c = m4.connect({str(tmp_path / 'mem.db')!r}); "
              f"m4.ingest_editions(c, file={dump!r}, progress_every=10000); c.close()")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    subprocess.run([sys.executable, "-c", script], check=True,
                   cwd="/", capture_output=True)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    assert peak <= max(before, MEM_BUDGET_KB), \
        f"peak child RSS {peak:,} KB exceeds {MEM_BUDGET_KB:,} KB budget"
    import sqlite3
    conn = sqlite3.connect(tmp_path / "mem.db")
    assert conn.execute("SELECT COUNT(*) FROM editions_ref").fetchone()[0] == 50_000
    conn.close()


# ---------------------------------------------------------------- CLI smoke
def test_cli_m4_subcommands_render_and_run(tmp_path, capsys):
    write_gz(tmp_path / "works.txt.gz", works_lines())
    write_gz(tmp_path / "authors.txt.gz", authors_lines())
    write_gz(tmp_path / "editions.txt.gz", edge_editions_lines())
    db = str(tmp_path / "cli.db")
    for argv in [
        ["ingest-works", "--works", str(tmp_path / "works.txt.gz"),
         "--authors", str(tmp_path / "authors.txt.gz")],
        ["ingest-editions", "--file", str(tmp_path / "editions.txt.gz")],
        ["gen-candidates", "--max-editions", "1"],
        ["export-list", "--top", "5", "--csv", str(tmp_path / "l.csv"),
         "--md", str(tmp_path / "l.md")],
    ]:
        assert cli_main(["--db", db] + argv) == 0
    out = capsys.readouterr().out
    assert "7 ISBN-keyed rows" in out and "candidate(s)" in out
    # --editions-dump parses and backfills winners' titles into the CSV
    rc = cli_main(["--db", db, "export-list", "--top", "100",
                   "--csv", str(tmp_path / "bt.csv"),
                   "--editions-dump", str(tmp_path / "editions.txt.gz")])
    assert rc == 0
    assert f"{isbn13_for(1)},Poemas Açorianos,Maria Fonseca" in \
        (tmp_path / "bt.csv").read_text(encoding="utf-8")
    # mutually exclusive source args rejected (argparse exits 2)
    with pytest.raises(SystemExit):
        cli_main(["--db", db, "ingest-editions"])
    with pytest.raises(SystemExit):
        cli_main(["--db", db, "ingest-editions", "--file", "x", "--stream-url", "u"])
