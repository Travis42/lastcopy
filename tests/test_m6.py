"""M6 — digital-rescue sources tests (SPEC-M6-RESCUE).

Phase A: pg_catalog CSV fixture + title/author matcher (normalization,
±2yr, surname-only) + Gutendex stub ({count, results:[...]}).
Phase B: OAI XML fixtures (resumptionToken loop, dc:relation ark pairs,
dc ISBN records) for both harvests; join logic (ark chains); workset
match; NT reclass integration; idempotent re-harvest (skip via
checkpoint/done marker); UA requirement (stub asserts header).
No network access.
"""

from __future__ import annotations

import gzip
import json

import pytest

from lastcopy import m4, m5, m6
from lastcopy.cli import main as cli_main
from lastcopy.isbn import isbn13_to_10

from .test_m5 import FakeResponse, isbn13_for


# ------------------------------------------------------------------ helpers
class FakeOAI:
    """Serves canned OAI page XML by expected params; records calls/sleeps.
    get(url, params, headers=None) — the m6 transport signature."""

    def __init__(self, pages: list[tuple[dict, str]] | None = None):
        self.pages = list(pages or [])
        self.calls: list[dict] = []
        self.sleeps: list[float] = []
        self.by_token: dict[str, str] = {}
        for params, text in self.pages:
            if "resumptionToken" in params:
                self.by_token[params["resumptionToken"]] = text

    def get(self, url, params, headers=None):
        self.calls.append({"url": url, "params": dict(params),
                           "headers": dict(headers or {})})
        if "resumptionToken" in params:
            return FakeXMLResponse(self.by_token[params["resumptionToken"]])
        return FakeXMLResponse(self.pages.pop(0)[1])

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)


class FakeXMLResponse:
    def __init__(self, text: str):
        self.status_code = 200
        self.text = text

    @property
    def ok(self) -> bool:
        return True


def gutendex_hit(pg_id: int) -> FakeResponse:
    return FakeResponse(200, {"count": 1, "results": [{"id": pg_id}]})


GUTENDEX_MISS = FakeResponse(200, {"count": 0, "results": []})


class FakeGutendexHTTP:
    """get(url, params) stub for m5._request_json-style calls."""

    def __init__(self, by_isbn: dict[str, FakeResponse]):
        self.by_isbn = by_isbn
        self.calls: list[dict] = []

    def get(self, url, params):
        self.calls.append({"url": url, "params": dict(params)})
        return self.by_isbn[params["isbn"]]


def seed_workset(conn, rows):
    """rows = [(isbn13, title, author_name, year)] -> workset + status +
    candidates + works_ref/authors_ref (the matcher's bib-data joins)."""
    m5.ensure_schema(conn)
    for i, (i13, title, author, year) in enumerate(rows):
        wk = f"/works/M6W{i}"
        ak = f"/authors/OL{100 + i}A" if author else None
        conn.execute("INSERT INTO enrich_workset (isbn13, work_key, "
                     "edition_count, score, oclc, created_at) "
                     "VALUES (?,?,1,5,NULL,?)", (i13, wk, m5.now()))
        conn.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (i13,))
        conn.execute("INSERT OR REPLACE INTO candidates (isbn13, work_key, "
                     "title, year, edition_count, score) "
                     "VALUES (?,?,?,?,1,5)", (i13, wk, title, year))
        conn.execute("INSERT OR REPLACE INTO works_ref (work_key, "
                     "edition_count, author_keys) VALUES (?,0,?)",
                     (wk, json.dumps([ak]) if ak else None))
        if ak:
            conn.execute("INSERT OR REPLACE INTO authors_ref (author_key, "
                         "name) VALUES (?,?)", (ak, author))
    conn.commit()
    return conn


@pytest.fixture()
def pg_catalog(tmp_path):
    p = tmp_path / "pg_catalog.csv"
    p.write_text("Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
                 "1342,Text,1998-10-01,\"Pride and Prejudice\",en,"
                 "\"Austen, Jane, 1775-1817\",PR4034,PR,\n"
                 "2701,Sound,2002-01-01,\"Mobby Dick audio\",en,\"Melville, Herman\",PS,,\n"
                 "2701,Text,2002-01-01,\"Moby Dick; or, The Whale\",en,"
                 "\"Melville, Herman, 1819-1891\",PS,,\n"
                 "84,Text,1993-01-01,\"Frankenstein\",en,"
                 "\"Shelley, Mary\",PR,,\n"
                 "9999,Text,2001-05-05,\"Frankenstein\",en,"
                 "\"Shelley, Mary Wollstonecraft\",,,\n", encoding="utf-8")
    return p


# ------------------------------------------------------------- matcher
def test_norm_title_articles_punct_accents():
    assert m6.norm_title("The Winter's Tale") == "winters tale"
    assert m6.norm_title("The Winters Tale") == "winters tale"
    assert m6.norm_title("L'Étranger") == "letranger"
    assert m6.norm_title("  A   Tale  of  Two  Cities! ") == \
        m6.norm_title("Tale of Two Cities")
    assert m6.norm_title("") == "" and m6.norm_title(None) == ""


def test_author_surname_forms():
    assert m6.author_surname("Austen, Jane, 1775-1817") == "austen"
    assert m6.author_surname("Jane Austen") == "austen"
    assert m6.author_surname("Mary Wollstonecraft Shelley") == "shelley"
    assert m6.author_surname("Hugo, Victor; Gautier, Théophile") == "hugo"
    assert m6.author_surname("Émile Zola") == "zola"
    assert m6.author_surname(None) == ""


def test_matcher_surname_only_year_window():
    rows = [
        {"id": "1", "title": "Frankenstein", "authors": "Shelley, Mary W.",
         "year": 1818},
        {"id": "2", "title": "Frankenstein", "authors": "Shelley, Mary W.",
         "year": 1831},
        {"id": "3", "title": "Frankenstein", "authors": "Shelley, Mary W.",
         "year": 1900},
    ]
    # 1818 work-year: 1818 & 1831 within +-2? 1831 is 13 off -> only id 1
    assert m6.match_pg("Frankenstein", "Mary Wollstonecraft Shelley", 1818,
                       rows) == ["1"]
    # year unknown on the workset side -> all title+surname matches returned
    assert m6.match_pg("Frankenstein", "Shelley, Mary", None, rows) == \
        ["1", "2", "3"]
    # +-2 window: 1820 matches 1818 only
    assert m6.match_pg("Frankenstein", "Shelley", 1820, rows) == ["1"]
    # 1833 -> within 2 of 1831 only
    assert m6.match_pg("Frankenstein", "Shelley", 1833, rows) == ["2"]
    # different author surname -> no match
    assert m6.match_pg("Frankenstein", "Jane Shelley", 1818,
                       [{"id": "9", "title": "Frankenstein",
                         "authors": "Austen, Jane", "year": 1818}]) == []


def test_load_pg_catalog_filters_non_text(pg_catalog):
    rows = m6.load_pg_catalog(pg_catalog)
    ids = [r["id"] for r in rows]
    assert ids == ["1342", "2701", "84", "9999"]   # Sound row dropped


# ------------------------------------------------------- enrich-gutenberg
def test_enrich_gutenberg_unique_ambiguous_gutendex(tmp_path, pg_catalog):
    conn = m4.connect(tmp_path / "m6.db")
    i_uniq, i_amb, i_miss = isbn13_for(1), isbn13_for(2), isbn13_for(3)
    seed_workset(conn, [
        # "Moby Dick; or, The Whale" — unique catalog row (Sound excluded)
        (i_uniq, "Moby Dick, or, The Whale", "Herman Melville", 1851),
        # "Frankenstein" — TWO catalog rows (84 + 9999) -> ambiguous
        (i_amb, "Frankenstein", "Mary Shelley", 1818),
        # no catalog match at all
        (i_miss, "Some Obscure Title", "Nobody Nowhere", 1955),
    ])
    http = FakeGutendexHTTP({i_amb: gutendex_hit(84), i_miss: GUTENDEX_MISS})
    stats = m6.enrich_gutenberg(conn, pg_catalog, get=http.get,
                                sleep=lambda s: None)
    assert stats["pg_rows"] == 4
    got = dict(conn.execute("SELECT isbn13, pg_id FROM enrich_status"
                            ).fetchall())
    assert got[i_uniq] == "2701"        # unique match, no Gutendex call
    assert got[i_amb] == "84"           # Gutendex authoritative hit
    assert got[i_miss] is None
    # Gutendex queried ONLY for the ambiguous row (miss row has no PG
    # candidates, so no query per spec)
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == m6.GUTENDEX_URL
    assert http.calls[0]["params"] == {"isbn": i_amb}
    # 'pg' marked checked on every row
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%pg%'"
                        ).fetchone()["c"] == 3
    # idempotent: rerun scans nothing, sets nothing new
    stats2 = m6.enrich_gutenberg(conn, pg_catalog, get=http.get,
                                 sleep=lambda s: None)
    assert stats2["rows_scanned"] == 1 and stats2["unique"] == 0
    assert len(http.calls) == 1
    conn.close()


def test_enrich_gutenberg_nt_reclass_and_custody_open(tmp_path, pg_catalog):
    conn = m4.connect(tmp_path / "m6nt.db")
    i13 = isbn13_for(7)
    seed_workset(conn, [(i13, "Pride and Prejudice", "Jane Austen", 1813)])
    m6.enrich_gutenberg(conn, pg_catalog, get=lambda u, p: GUTENDEX_MISS,
                        sleep=lambda s: None)
    m5.assign_status(conn)
    row = conn.execute("SELECT status, status_basis, custody, pg_id "
                       "FROM enrich_status").fetchone()
    assert row["status"] == "NT"
    assert "pg hit" in row["status_basis"]
    assert row["custody"] == "open"          # free digital (Gallica same rule)
    assert row["pg_id"] == "1342"
    conn.close()


# ---------------------------------------------------------- OAI fixtures
def oai_page(records_xml: str, token: str | None) -> str:
    tok = (f"<resumptionToken>{token}</resumptionToken>" if token
           else "<resumptionToken/>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            "<ListRecords>" + records_xml + tok + "</ListRecords></OAI-PMH>")


NUM_REC = """<record><metadata><oai_dc:dc
 xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:identifier>http://gallica.bnf.fr/ark:/12148/bpt6k56516392</dc:identifier>
 <dc:relation>Notice du catalogue : http://catalogue.bnf.fr/ark:/12148/cb301272811</dc:relation>
 <dc:title>Orgueil et préjugés</dc:title>
</oai_dc:dc></metadata></record>"""

NUM_REC2 = """<record><metadata><oai_dc:dc
 xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:identifier>http://gallica.bnf.fr/ark:/12148/btv1b10516265w</dc:identifier>
 <dc:relation>http://catalogue.bnf.fr/ark:/12148/cb42684349k</dc:relation>
</oai_dc:dc></metadata></record>"""

CAT_REC = """<record><header>
 <identifier>oai:bnf.fr:catalogue/ark:/12148/cb41003094h</identifier>
</header><metadata><oai_dc:dc
 xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:identifier>ISBN 9782915673142</dc:identifier>
 <dc:identifier>http://catalogue.bnf.fr/ark:/12148/cb41003094h/description</dc:identifier>
 <dc:title>Some catalogue notice</dc:title>
</oai_dc:dc></metadata></record>"""


def test_parse_oai_page_num_pairs_and_token():
    pairs, token, n = m6.parse_oai_page(oai_page(NUM_REC, "TOK123"))
    assert pairs == [("bpt6k56516392", "cb301272811")]
    assert token == "TOK123" and n == 1
    pairs, token, n = m6.parse_oai_page(oai_page(NUM_REC2, None))
    assert pairs == [("btv1b10516265w", "cb42684349k")]
    assert token is None and n == 1


def test_parse_oai_page_cat_isbn_pairs():
    pairs, token, n = m6.parse_oai_page(oai_page(CAT_REC, "CAT1"))
    assert pairs == [("cb41003094h", "9782915673142")]
    assert token == "CAT1" and n == 1
    # a record with cb but no ISBN yields nothing
    no_isbn = CAT_REC.replace(
        "<dc:identifier>ISBN 9782915673142</dc:identifier>", "")
    assert m6.parse_oai_page(oai_page(no_isbn, None))[0] == []


def test_harvest_gallica_num_token_loop_ua_and_done(tmp_path):
    http = FakeOAI([
        ({"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": "gallica"},
         oai_page(NUM_REC, "T1")),
        ({"verb": "ListRecords", "resumptionToken": "T1"},
         oai_page(NUM_REC2, None)),
    ])
    stats = m6.harvest_gallica("num", tmp_path, get=http.get,
                               sleep=http.sleep, min_interval=0)
    assert stats["done"] is True and stats["pages"] == 2
    assert stats["records"] == 2 and stats["pairs"] == 2
    # MANDATORY browser UA on every request (default UA gets 403)
    assert all(c["headers"]["User-Agent"] == m6.BROWSER_UA for c in http.calls)
    assert m6.BROWSER_UA.startswith("Mozilla/5.0")
    # first page: full params; second page: resumptionToken EXCLUSIVE
    assert http.calls[0]["params"] == {"verb": "ListRecords",
                                       "metadataPrefix": "oai_dc",
                                       "set": "gallica"}
    assert http.calls[1]["params"] == {"verb": "ListRecords",
                                       "resumptionToken": "T1"}
    assert http.calls[0]["url"] == m6.OAI_ENDPOINTS["num"]["url"]
    assert all(s == 0 for s in http.sleeps)     # min_interval honored
    # pairs persisted to gzip TSV
    lines = gzip.decompress(
        (tmp_path / "num_pairs.tsv.gz").read_bytes()).decode().splitlines()
    assert lines == ["bpt6k56516392\tcb301272811",
                     "btv1b10516265w\tcb42684349k"]
    assert (tmp_path / "num.done").exists()
    # idempotent re-harvest: done marker -> zero requests
    stats2 = m6.harvest_gallica("num", tmp_path, get=http.get,
                                sleep=http.sleep, min_interval=0)
    assert stats2["skipped_done"] is True and len(http.calls) == 2


def test_harvest_gallica_checkpoint_resume(tmp_path):
    http1 = FakeOAI([
        ({"verb": "ListRecords", "metadataPrefix": "oai_dc",
          "set": "catalogue:edition:livres"}, oai_page(CAT_REC, "RES1")),
    ])
    stats = m6.harvest_gallica("cat", tmp_path, get=http1.get,
                               sleep=http1.sleep, min_interval=0,
                               max_pages=1)
    assert stats["done"] is False and stats["pages"] == 1
    ck = json.loads((tmp_path / "cat.ckpt").read_text())
    assert ck["token"] == "RES1" and ck["pages"] == 1
    assert not (tmp_path / "cat.done").exists()
    # rerun resumes FROM the token (no fresh set query), finishes
    http2 = FakeOAI([( {"verb": "ListRecords", "resumptionToken": "RES1"},
                       oai_page(CAT_REC, None))])
    stats2 = m6.harvest_gallica("cat", tmp_path, get=http2.get,
                                sleep=http2.sleep, min_interval=0)
    assert stats2["resumed"] is True and stats2["done"] is True
    assert http2.calls[0]["params"] == {"verb": "ListRecords",
                                        "resumptionToken": "RES1"}
    assert http2.calls[0]["url"] == m6.OAI_ENDPOINTS["cat"]["url"]
    # duplicate pair from the resumed page is fine — join dedups
    lines = gzip.decompress(
        (tmp_path / "cat_pairs.tsv.gz").read_bytes()).decode().splitlines()
    assert lines.count("cb41003094h\t9782915673142") == 2


def test_harvest_gallica_retry_on_403(tmp_path):
    """Default-UA 403s must back off and retry (browser UA is mandatory but
    transient 403s still occur per the research notes)."""
    attempts = {"n": 0}

    class Flaky:
        def __init__(self):
            self.sleeps = []

        def get(self, url, params, headers=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return FakeResponse(403)
            return FakeXMLResponse(oai_page(NUM_REC, None))

    flaky = Flaky()
    stats = m6.harvest_gallica("num", tmp_path, get=flaky.get,
                               sleep=flaky.sleeps.append, min_interval=0,
                               max_retries=3)
    assert stats["done"] is True and attempts["n"] == 2
    assert flaky.sleeps == [0, 1.0, 0]                # backoff 2^0 after 403


# ------------------------------------------------------------ parse-gallica
def _pairs_file(tmp_path, name: str, pairs: list[tuple[str, str]]) -> str:
    p = tmp_path / name
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("".join(f"{a}\t{b}\n" for a, b in pairs))
    return str(p)


def test_parse_gallica_ark_chain_join_and_nt(tmp_path):
    conn = m4.connect(tmp_path / "join.db")
    i_hit, i_miss = isbn13_for(1), isbn13_for(2)
    i10_fold = isbn13_for(3)
    seed_workset(conn, [(i_hit, "Orgueil et préjugés", "Austen, Jane", 1813),
                        (i_miss, "Unmatched", "X, Y", 1900),
                        (i10_fold, "Other", "Z, W", 1920)])
    num = _pairs_file(tmp_path, "num_pairs.tsv.gz", [
        ("bpt6k56516392", "cb301272811"),
        ("btv1b10516265w", "cb42684349k"),
    ])
    cat = _pairs_file(tmp_path, "cat_pairs.tsv.gz", [
        ("cb301272811", i_hit),
        ("cb301272811", isbn13_to_10(i10_fold)),   # ISBN-10 folds to 13
        ("cb42684349k", isbn13_for(99)),           # ISBN not in workset
        ("cbNOGAL", i_miss),                       # cb with no gallica ark
    ])
    stats = m6.parse_gallica(conn, num, cat)
    assert stats["matched"] == 2
    got = dict(conn.execute("SELECT isbn13, gallica_ark FROM enrich_status"
                            ).fetchall())
    assert got[i_hit] == "bpt6k56516392"
    assert got[i10_fold] == "bpt6k56516392"        # isbn10 normalized onto 13
    assert got[i_miss] is None
    assert conn.execute("SELECT COUNT(*) c FROM enrich_status "
                        "WHERE sources_checked LIKE '%gallica%'"
                        ).fetchone()["c"] == 3
    # NT reclass + custody open via the status chain
    m5.assign_status(conn)
    rows = {r["isbn13"]: r for r in conn.execute(
        "SELECT isbn13, status, status_basis, custody FROM enrich_status")}
    assert rows[i_hit]["status"] == "NT"
    assert "gallica hit" in rows[i_hit]["status_basis"]
    assert rows[i_hit]["custody"] == "open"
    assert rows[i_miss]["status"] != "NT" or rows[i_miss]["custody"] != "open"
    # idempotent
    stats2 = m6.parse_gallica(conn, num, cat)
    assert stats2["matched"] == 0 and stats2["gallica_ark_set"] == 2
    conn.close()


def test_resolved_rows_skip_ia_plan(tmp_path):
    """PG/Gallica-rescued rows count as resolved: the IA plan must not
    re-query them (same semantics as HT/WD)."""
    conn = m4.connect(tmp_path / "plan.db")
    i_pg, i_gal, i_open = isbn13_for(1), isbn13_for(2), isbn13_for(3)
    seed_workset(conn, [(i_pg, "A", "B, C", 1900),
                        (i_gal, "D", "E, F", 1900),
                        (i_open, "G", "H, I", 1900)])
    conn.execute("UPDATE enrich_status SET pg_id='1342' WHERE isbn13=?",
                 (i_pg,))
    conn.execute("UPDATE enrich_status SET gallica_ark='bpt6x' WHERE isbn13=?",
                 (i_gal,))
    conn.commit()
    assert m5.build_ia_plan(conn)["isbn_isbns"] == 1   # only i_open
    conn.close()


# ------------------------------------------------------------- CLI smoke
def test_cli_m6_subcommands(tmp_path, pg_catalog, capsys, monkeypatch):
    db = tmp_path / "cli.db"
    i13 = isbn13_for(1)
    conn = seed_workset(m4.connect(db), [
        (i13, "Pride and Prejudice", "Jane Austen", 1813)])
    conn.close()
    monkeypatch.setattr(m6, "_gutendex_lookup", lambda isbn13, **kw: "1342")
    assert cli_main(["--db", str(db), "enrich-gutenberg",
                     "--catalog", str(pg_catalog)]) == 0
    out = capsys.readouterr().out
    assert "enrich-gutenberg" in out and "pg_id set on 1" in out

    num = _pairs_file(tmp_path, "n.tsv.gz",
                      [("bpt6k56516392", "cb301272811")])
    cat = _pairs_file(tmp_path, "c.tsv.gz", [("cb301272811", i13)])
    assert cli_main(["--db", str(db), "parse-gallica",
                     "--num-file", num, "--cat-file", cat]) == 0
    out = capsys.readouterr().out
    assert "parse-gallica" in out and "matched 1" in out
    assert cli_main(["--db", str(db), "assign-status"]) == 0
    conn = m4.connect(db)
    row = conn.execute("SELECT status, custody, pg_id, gallica_ark "
                       "FROM enrich_status").fetchone()
    assert row["status"] == "NT" and row["custody"] == "open"
    assert row["pg_id"] == "1342" and row["gallica_ark"] == "bpt6k56516392"
    conn.close()
    # workset export carries the new columns
    csvp = tmp_path / "ws.csv"
    assert cli_main(["--db", str(db), "export-list", "--workset",
                     "--csv", str(csvp)]) == 0
    hdr = csvp.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert hdr[-2:] == ["pg_id", "gallica_ark"]


def test_cli_fetch_gutenberg_file_url(tmp_path, capsys):
    """Real curl path exercised offline via a file:// URL; bad CSV fails."""
    src = tmp_path / "cat.csv"
    src.write_text("Text#,Type,Issued,Title,Language,Authors\n"
                   "1342,Text,1998-10-01,Pride and Prejudice,en,Austen\n",
                   encoding="utf-8")
    dest = tmp_path / "gut"
    rc = cli_main(["fetch-gutenberg", "--data-dir", str(dest),
                   "--url", src.as_uri()])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fetch-gutenberg" in out and "1 rows" in out
    assert (dest / "pg_catalog.csv").exists()
    # unparseable CSV -> rc 1 with the integrity error
    bad = tmp_path / "bad.csv"
    bad.write_text("Text#,Type\n1\t2\n\"unbalanced", encoding="utf-8")
    rc2 = cli_main(["fetch-gutenberg", "--data-dir", str(tmp_path / "bad"),
                    "--url", bad.as_uri()])
    assert rc2 == 1
    assert "error" in capsys.readouterr().err


def test_cli_fetch_gallica_renders(tmp_path, capsys):
    # completed harvest (done marker) -> skipped, zero requests
    (tmp_path / "num.done").write_text("t\n", encoding="utf-8")
    rc = cli_main(["fetch-gallica", "--stage", "num",
                   "--data-dir", str(tmp_path)])
    assert rc == 0
    assert "skipped" in capsys.readouterr().out
    # unknown stage -> clean error
    rc2 = cli_main(["fetch-gallica", "--stage", "bogus",
                    "--data-dir", str(tmp_path)])
    assert rc2 == 2
