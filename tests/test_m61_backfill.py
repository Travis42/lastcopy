"""M6.1 — workset title backfill tests (SPEC-M6.1-TITLE-BACKFILL).

Incident 14827: enrich-gutenberg silently scanned 0 rows because
candidates.title was NULL workset-wide; the only title backfill lived in
the top-N export (post-enrich) and export-list --workset silently ignored
--editions-dump.  Here: backfill_titles semantics (isbn_13 fill, isbn_10
fold, no-clobber, workset-only, multi-ISBN, malformed lines, idempotency),
the fail-loud enrich guard (both ways), and the CLI surfaces.
No network access.
"""

from __future__ import annotations

import pytest

from lastcopy import m4, m5, m6
from lastcopy.cli import main as cli_main
from lastcopy.isbn import isbn13_to_10

from .test_m4 import dump_line, write_gz
from .test_m5 import isbn13_for
from .test_m6 import GUTENDEX_MISS, seed_workset


# ------------------------------------------------------------ fixtures
def dump_file(tmp_path, lines, name="editions.txt.gz"):
    return write_gz(tmp_path / name, lines)


PG_CATALOG_CSV = (
    "Text#,Type,Issued,Title,Language,Authors\n"
    "1342,Text,1998-10-01,Pride and Prejudice,en,Austen, Jane\n")


@pytest.fixture()
def pg_catalog(tmp_path):
    p = tmp_path / "pg_catalog.csv"
    p.write_text(PG_CATALOG_CSV, encoding="utf-8")
    return p


# ------------------------------------------------------ backfill_titles
def test_backfill_isbn13_and_title_precedence(tmp_path):
    conn = m4.connect(tmp_path / "bf.db")
    i13, i_full = isbn13_for(1), isbn13_for(2)
    seed_workset(conn, [(i13, None, "A, B", 1900),
                        (i_full, None, "C, D", 1910)])
    dump = dump_file(tmp_path, [
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "Plain Title", "isbn_13": [i13]}),
        # no `title` -> full_title; no full_title -> subtitle
        dump_line("/type/edition", "/books/OL2M",
                  {"full_title": "Full Wins", "isbn_13": [i_full]}),
    ])
    stats = m6.backfill_titles(conn, dump)
    assert stats == {"records_read": 2, "workset_isbns": 2,
                     "titles_filled": 2, "previously_filled": 0}
    got = dict(conn.execute("SELECT isbn13, title FROM candidates").fetchall())
    assert got[i13] == "Plain Title" and got[i_full] == "Full Wins"
    conn.close()


def test_backfill_isbn10_fold(tmp_path):
    conn = m4.connect(tmp_path / "bf10.db")
    i13 = isbn13_for(3)
    seed_workset(conn, [(i13, None, "A, B", 1920)])
    dump = dump_file(tmp_path, [
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "Ten Digit", "isbn_10": [isbn13_to_10(i13)]}),
    ])
    stats = m6.backfill_titles(conn, dump)
    assert stats["titles_filled"] == 1
    assert conn.execute("SELECT title FROM candidates WHERE isbn13=?",
                        (i13,)).fetchone()["title"] == "Ten Digit"
    conn.close()


def test_backfill_no_clobber_and_titleless_skip(tmp_path):
    conn = m4.connect(tmp_path / "bfnc.db")
    i_set, i_null = isbn13_for(4), isbn13_for(5)
    seed_workset(conn, [(i_set, "Pre-set Title", "A, B", 1900),
                        (i_null, None, "C, D", 1910)])
    dump = dump_file(tmp_path, [
        # would clobber — must NOT (title already set)
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "Dump Title", "isbn_13": [i_set]}),
        # titleless record for the NULL row — skipped entirely
        dump_line("/type/edition", "/books/OL2M",
                  {"isbn_13": [i_null], "publishers": ["X"]}),
    ])
    stats = m6.backfill_titles(conn, dump)
    assert stats["titles_filled"] == 0
    assert stats["previously_filled"] == 1
    got = dict(conn.execute("SELECT isbn13, title FROM candidates").fetchall())
    assert got[i_set] == "Pre-set Title" and got[i_null] is None
    conn.close()


def test_backfill_non_workset_ignored_multi_isbn_fills_both(tmp_path):
    conn = m4.connect(tmp_path / "bfws.db")
    i_a, i_b = isbn13_for(6), isbn13_for(7)
    seed_workset(conn, [(i_a, None, "A, B", 1900),
                        (i_b, None, "C, D", 1910)])
    dump = dump_file(tmp_path, [
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "Multi", "isbn_13": [i_a, i_b,
                                                 isbn13_for(99)]}),
    ])
    stats = m6.backfill_titles(conn, dump)
    assert stats["titles_filled"] == 2      # isbn13_for(99) not in workset
    got = dict(conn.execute("SELECT isbn13, title FROM candidates").fetchall())
    assert got[i_a] == "Multi" and got[i_b] == "Multi"
    conn.close()


def test_backfill_idempotent_rerun(tmp_path):
    conn = m4.connect(tmp_path / "bfrerun.db")
    i13 = isbn13_for(8)
    seed_workset(conn, [(i13, None, "A, B", 1900)])
    dump = dump_file(tmp_path, [
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "First Wins", "isbn_13": [i13]}),
        dump_line("/type/edition", "/books/OL2M",
                  {"title": "Second Record", "isbn_13": [i13]}),
    ])
    stats = m6.backfill_titles(conn, dump)
    assert stats["titles_filled"] == 1      # first match wins per ISBN13
    title = conn.execute("SELECT title FROM candidates WHERE isbn13=?",
                         (i13,)).fetchone()["title"]
    assert title == "First Wins"
    stats2 = m6.backfill_titles(conn, dump)
    assert stats2["titles_filled"] == 0
    assert stats2["previously_filled"] == 1
    assert conn.execute("SELECT title FROM candidates WHERE isbn13=?",
                        (i13,)).fetchone()["title"] == "First Wins"
    conn.close()


def test_backfill_malformed_lines_skipped(tmp_path):
    conn = m4.connect(tmp_path / "bfbad.db")
    i13 = isbn13_for(9)
    seed_workset(conn, [(i13, None, "A, B", 1900)])
    dump = dump_file(tmp_path, [
        "no tabs at all",
        "/type/edition\t/books/OLBM\t1\tts\t{not json",
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "Good", "isbn_13": [i13]}),
    ])
    stats = m6.backfill_titles(conn, dump)
    assert stats["records_read"] == 3        # raw lines, bad included
    assert stats["titles_filled"] == 1
    conn.close()


# ----------------------------------------------------------- fail-loud guard
def test_guard_raises_on_null_titles_with_unchecked_rows(tmp_path, capsys):
    conn = m4.connect(tmp_path / "guard.db")
    seed_workset(conn, [(isbn13_for(1), None, "A, B", 1900)])  # title NULL
    (tmp_path / "pg.csv").write_text(PG_CATALOG_CSV, encoding="utf-8")
    with pytest.raises(RuntimeError, match="backfill-titles"):
        m6.enrich_gutenberg(conn, tmp_path / "pg.csv",
                            get=lambda u, p: GUTENDEX_MISS,
                            sleep=lambda s: None)
    # CLI surface: error to stderr, rc 1
    rc = cli_main(["--db", str(tmp_path / "guard.db"), "enrich-gutenberg",
                   "--catalog", str(tmp_path / "pg.csv")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "backfill-titles" in err
    conn.close()


def test_guard_does_not_raise_when_all_checked(tmp_path, pg_catalog):
    conn = m4.connect(tmp_path / "guard2.db")
    i13 = isbn13_for(2)
    seed_workset(conn, [(i13, "Some Title", "A, B", 1900)])
    conn.execute("UPDATE enrich_status SET pg_id='1342'")
    conn.commit()
    # idempotent rerun: 0 scanned, 0 unchecked -> no raise
    stats = m6.enrich_gutenberg(conn, pg_catalog,
                                get=lambda u, p: GUTENDEX_MISS,
                                sleep=lambda s: None)
    assert stats["rows_scanned"] == 0 and stats["pg_id_set"] == 1
    conn.close()


# ------------------------------------------------------------------ CLI
def test_cli_backfill_titles_end_to_end(tmp_path, capsys):
    db = tmp_path / "cli.db"
    i13 = isbn13_for(11)
    conn = seed_workset(m4.connect(db), [(i13, None, "A, B", 1900)])
    conn.close()
    dump = dump_file(tmp_path, [
        dump_line("/type/edition", "/books/OL1M",
                  {"title": "Via CLI", "isbn_13": [i13]}),
    ])
    rc = cli_main(["--db", str(db), "backfill-titles",
                   "--editions-dump", dump])
    assert rc == 0
    out = capsys.readouterr().out
    assert "backfill-titles:" in out and "1 titles filled" in out
    assert "1 already set" not in out
    conn = m4.connect(db)
    assert conn.execute("SELECT title FROM candidates WHERE isbn13=?",
                        (i13,)).fetchone()["title"] == "Via CLI"
    conn.close()


def test_cli_export_list_workset_plus_dump_is_error(tmp_path, capsys):
    db = tmp_path / "ws.db"
    i13 = isbn13_for(12)
    conn = seed_workset(m4.connect(db), [(i13, None, "A, B", 1900)])
    conn.close()
    rc = cli_main(["--db", str(db), "export-list", "--workset",
                   "--csv", str(tmp_path / "ws.csv"),
                   "--editions-dump", str(tmp_path / "x.txt.gz")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err and "backfill-titles" in err
