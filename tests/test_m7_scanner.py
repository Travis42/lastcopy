"""M7 — marketplace scanner pilot tests (SPEC-M7-SCANNER-PILOT).

Covers: stratified sampler (strata counts, wild-only, score order),
query builders (ISBN + title/author slug forms incl. umlauts/accents),
SERP parsers against the checked-in real-response fixture excerpts,
robots guard (unchanged -> proceed, tightened -> abort), per-source rate
limiter timing (fake sleep), transport retry discipline, sightings
schema + insert, and a CLI smoke run with a fake get.  No network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from lastcopy import m4, m7_scanner as m7
from lastcopy.m7_scanner import Book

from .test_m5 import isbn13_for

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHTML:
    def __init__(self, text: str, status: int = 200):
        self.status_code = status
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


# ------------------------------------------------------------------ sampler
def seed_pilot_db(tmp_path, *, n_ger=3, n_null=2, n_fre=1, n_und=2,
                   n_other=1, n_not_wild=2):
    """Fixture M4/M5 DB with MARC language codes (postmortem 2026-10-06:
    the wild-CR population runs ger / NULL / fre / und, ~0 eng): wild CR
    books across strata (score descending by construction) + non-wild
    rows that must NEVER sample."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = m4.connect(tmp_path / "m7.db")
    from lastcopy import m5
    m5.ensure_schema(conn)
    rows: list[tuple[str, str, int, str, bool]] = []
    i = 0
    for lang, n in (("ger", n_ger), (None, n_null), ("fre", n_fre),
                    ("und", n_und), ("ita", n_other)):
        for _ in range(n):
            i += 1
            rows.append((isbn13_for(i), lang, 100 - i,
                         f"Wild Book {i} ({lang or 'null'})", True))
    for _ in range(n_not_wild):
        i += 1
        rows.append((isbn13_for(i), "ger", 1000 - i, "Not wild", False))
    for i13, lang, score, title, wild in rows:
        wk = f"/works/M7W{i13}"
        conn.execute(
            "INSERT OR REPLACE INTO candidates (isbn13, work_key, title, "
            "year, language, edition_count, score) VALUES (?,?,?,?,?,1,?)",
            (i13, wk, title, 1910, lang, score))
        conn.execute(
            "INSERT OR REPLACE INTO enrich_workset (isbn13, work_key, "
            "edition_count, score, oclc, created_at) VALUES (?,?,1,?,NULL,?)",
            (i13, wk, score, "2026-10-06T00:00:00+00:00"))
        conn.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (i13,))
        if wild:
            conn.execute("UPDATE enrich_status SET status='CR', "
                         "custody_physical='wild', holdings='' WHERE isbn13=?",
                         (i13,))
        else:   # CR but with holdings -> safe-but-not-really-safe, not wild
            conn.execute("UPDATE enrich_status SET status='CR', "
                         "custody_physical='single', holdings='bnf' "
                         "WHERE isbn13=?", (i13,))
    conn.commit()
    return conn


def test_build_pilot_sample_strata_counts_and_wild_only(tmp_path):
    conn = seed_pilot_db(tmp_path)
    books = m7.build_pilot_sample(conn, 10)   # quotas 4/2/2/2
    langs = [b.language for b in books]
    assert langs.count("ger") == 3            # min(quota, available)
    assert langs.count("null") == 2
    assert langs.count("fre") == 1
    assert langs.count("und") == 2
    assert len(books) == 8
    # full-SPEC quotas with enough rows: exact 200 ger/100 NULL/100 fre/
    # 100 und+other (und 60 first, then misc tops up the last 40)
    conn2 = seed_pilot_db(tmp_path / "big", n_ger=210, n_null=110, n_fre=110,
                          n_und=60, n_other=40)
    books2 = m7.build_pilot_sample(conn2, 500)
    langs2 = [b.language for b in books2]
    assert (langs2.count("ger"), langs2.count("null"), langs2.count("fre")) \
        == (200, 100, 100)
    assert langs2.count("und") == 60          # und gets first shot
    assert len(books2) == 500                 # und 60 + other-misc 40
    # score DESC within strata + wild-only (not-wild rows score 1000 absent)
    ger_scores = [b.score for b in books2 if b.language == "ger"]
    assert ger_scores == sorted(ger_scores, reverse=True)
    assert all(b.score < 900 for b in books2)
    # sample n scales strata
    books3 = m7.build_pilot_sample(conn2, 50)
    assert len(books3) == 50
    conn.close()
    conn2.close()


def test_build_pilot_sample_skips_non_cr(tmp_path):
    conn = seed_pilot_db(tmp_path, n_ger=1, n_null=0, n_fre=0, n_und=0,
                         n_other=0)
    # flip the only ger row to NT (rescued) — the ger stratum goes empty
    # and the fail-loud guard fires (postmortem item 2)
    conn.execute("UPDATE enrich_status SET status='NT' WHERE isbn13=("
                 "SELECT isbn13 FROM candidates WHERE language='ger')")
    conn.commit()
    with pytest.raises(m7.StratumShortfallError) as exc:
        m7.build_pilot_sample(conn, 500)
    assert "ger" in str(exc.value)
    conn.close()


def test_sampler_guard_raises_with_shortfall_table(tmp_path):
    # fre population collapsed to 10 rows (quota 100 -> <50%): raise,
    # with the per-stratum shortfall table naming fre (postmortem item 2)
    conn = seed_pilot_db(tmp_path, n_ger=210, n_null=110, n_fre=10,
                         n_und=110, n_other=40)
    with pytest.raises(m7.StratumShortfallError) as exc:
        m7.build_pilot_sample(conn, 500)
    msg = str(exc.value)
    assert "fre: got 10/100" in msg
    assert "ger" not in msg.splitlines()[1]   # healthy strata not blamed
    conn.close()


# ------------------------------------------------------------ query builders
def test_build_query_isbn_and_title_forms():
    with_isbn = Book("9780141439518", "Pride and Prejudice", "Austen, Jane",
                     1813, "eng", 5)
    no_isbn = Book(None, "Outlines of Social Philosophy",
                   "Spencer, Herbert", 1851, "eng", 5)
    assert m7.build_query("abebooks", with_isbn) == (
        "https://www.abebooks.com/book-search/isbn/9780141439518/", "isbn")
    assert m7.build_query("abebooks", no_isbn) == (
        "https://www.abebooks.com/book-search/title/"
        "outlines-of-social-philosophy/author/spencer/", "title")
    assert m7.build_query("zvab", with_isbn) == (
        "https://www.zvab.com/buch-suchen/isbn/9780141439518/", "isbn")
    assert m7.build_query("zvab", no_isbn) == (
        "https://www.zvab.com/buch-suchen/titel/"
        "outlines-of-social-philosophy/autor/spencer/", "title")
    assert m7.build_query("antiqbook", with_isbn) == (
        "https://www.antiqbook.com/search?q=9780141439518", "isbn")
    url, kind = m7.build_query("antiqbook", no_isbn)
    assert (url, kind) == ("https://www.antiqbook.com/search?q="
                           "Outlines%20of%20Social%20Philosophy%20spencer",
                           "title")


def test_slug_folds_umaults_and_accents():
    assert m7.slug("Outlines of Social Philosophy") == \
        "outlines-of-social-philosophy"
    assert m7.slug("Übungsbuch der Physik") == "ubungsbuch-der-physik"
    assert m7.slug("L'Étranger!") == "l-etranger"
    assert m7.slug("  Mémoires  d'Outre--Tombe ") == "memoires-d-outre-tombe"
    assert m7.slug("") == "" and m7.slug(None) == ""
    # author segment takes the surname only (comma or free form)
    url, _ = m7.build_query("zvab", Book(
        None, "Betrachtungen", "Müller, Liesel, 1899-1971", 1920, "deu", 1))
    assert url.endswith("/autor/muller/")


# ----------------------------------------------------------------- parsers
def test_parse_abebooks_isbn_fixture():
    r = m7.parse_abebooks(fixture("abebooks_isbn.html"))
    assert r.n_results == 111                # result-count span marker
    assert r.top_price == pytest.approx(3.99)   # min first-page JSON-LD price
    assert r.currency == "USD"
    assert r.listing_title == "Pride and Prejudice (Penguin Classics)"


def test_parse_abebooks_title_fixture():
    r = m7.parse_abebooks(fixture("abebooks_title.html"))
    assert r.n_results == 9
    assert r.top_price == pytest.approx(3.54)
    assert r.currency == "USD"
    assert r.listing_title == "Outlines of Social Philosophy"


def test_parse_abebooks_zero_hit_without_marker():
    r = m7.parse_abebooks("<html>No marker here</html>")
    assert r.n_results == 0 and r.top_price is None
    # the OLD marker is gone from the live page: a stale totalResults
    # template string (plural rule, no number) must NOT count as a hit
    r2 = m7.parse_abebooks(
        '"showResults":"{totalResults, plural, =0 {No results} '
        'other {Show {totalResults} results}}"')
    assert r2.n_results == 0


def test_parse_zvab_fixture():
    r = m7.parse_zvab(fixture("zvab_isbn.html"))
    assert r.n_results == 46                  # "46 Ergebnisse"
    assert r.top_price == pytest.approx(4.95)  # EUR<nbp>4,95 (comma decimal)
    assert r.currency == "EUR"
    assert r.listing_title is not None


def test_parse_zvab_price_forms():
    # postmortem item 4: 'EUR\u00a020' (nbsp) joins '€ 20' and 'EUR 20'
    text = ("3 Ergebnisse"
            '<span class="price">EUR 20</span>'
            '<span class="price">€ 12,50</span>'
            '<span class="price">EUR 8</span>')
    r = m7.parse_zvab(text)
    assert r.n_results == 3
    assert r.top_price == pytest.approx(8.0)  # min across all three forms
    assert r.currency == "EUR"


def test_parse_antiqbook_fixture():
    r = m7.parse_antiqbook(fixture("antiqbook_q.html"))
    assert r.n_results == 1                   # "1 antiquarian books"
    assert r.top_price == pytest.approx(14.50)
    assert r.currency == "EUR"
    assert r.listing_title == "Pride and Prejudice"


def test_parse_zvab_zero_hit():
    r = m7.parse_zvab("<html>Keine…</html>")
    assert r.n_results == 0 and r.top_price is None and r.currency is None


# ------------------------------------------------------------- robots guard
class RobotsGet:
    """Serves robots fixtures by host; records every fetch."""

    def __init__(self, robots_by_source: dict[str, str]):
        self.robots = robots_by_source
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        for src, text in self.robots.items():
            if url == m7.SOURCES[src]["robots"]:
                return FakeHTML(text)
        return FakeHTML("", status=404)


def test_check_robots_unchanged_proceeds():
    http = RobotsGet({src: fixture(f"robots_{src}.txt")
                      for src in m7.DEFAULT_SOURCES})
    verdicts = m7.check_robots(m7.DEFAULT_SOURCES, http.get)
    assert set(http.urls) == {m7.SOURCES[s]["robots"] for s
                              in m7.DEFAULT_SOURCES}   # fetched FIRST, all
    assert all(v["ok"] for v in verdicts.values())
    assert "/book-search/isbn/" in verdicts["abebooks"]["reason"]


def test_check_robots_tightened_aborts_source():
    http = RobotsGet({
        "abebooks": fixture("robots_abebooks_tightened.txt"),
        "zvab": fixture("robots_zvab.txt"),
        "antiqbook": fixture("robots_antiqbook.txt")})
    verdicts = m7.check_robots(m7.DEFAULT_SOURCES, http.get)
    assert verdicts["abebooks"]["ok"] is False
    assert "TIGHTENED" in verdicts["abebooks"]["reason"]
    assert "/book-search/" in verdicts["abebooks"]["reason"]
    assert verdicts["zvab"]["ok"] and verdicts["antiqbook"]["ok"]


def test_check_robots_unreadable_aborts():
    def bad_get(url):
        return FakeHTML("cloudflare interstitial", status=403)
    verdicts = m7.check_robots(("abebooks",), bad_get)
    assert verdicts["abebooks"]["ok"] is False


# -------------------------------------------------------------- rate limiter
def test_rate_limiter_per_source_interval():
    t = {"now": 0.0}
    slept: list[float] = []

    def clock():
        return t["now"]

    def sleep(s):
        slept.append(s)
        t["now"] += s              # simulated clock advances while sleeping

    lim = m7.RateLimiter(min_interval=2.0, clock=clock, sleep=sleep)
    t["now"] = 100.0
    marks: dict[str, list[float]] = {}
    for src in ("abebooks", "zvab", "abebooks", "zvab", "abebooks"):
        lim.wait(src)
        marks.setdefault(src, []).append(t["now"])   # request start time
        t["now"] += 0.3            # the request itself takes 0.3 s
    # interleave a,z,a,z,a: 0.6 s elapses between same-source turns ->
    # each wait tops the gap back up to the full 2.0 s interval
    assert slept == [pytest.approx(1.4), pytest.approx(1.4)]
    for times in marks.values():
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert all(g >= 2.0 for g in gaps)


# ------------------------------------------------------------ transport retry
def test_serp_request_retries_transport_and_429():
    state = {"n": 0}
    sleeps: list[float] = []

    class Flaky:
        def get(self, url):
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectError("connection reset by peer")
            if state["n"] == 2:
                return FakeHTML("<html>slow down</html>", status=429)
            return FakeHTML(fixture("abebooks_isbn.html"))

    lim = m7.RateLimiter(min_interval=0, clock=lambda: 0.0,
                         sleep=lambda s: None)
    text = m7._serp_request(Flaky().get, "https://x/", lim, "abebooks",
                            sleeps.append)
    assert text is not None and "result-count" in text
    assert sleeps == [1.0, 2.0]           # 2^0 then 2^1 backoff


def test_serp_request_gives_up_after_max_retries():
    sleeps: list[float] = []

    class Dead:
        def get(self, url):
            raise httpx.ReadError("reset")

    lim = m7.RateLimiter(min_interval=0, clock=lambda: 0.0,
                         sleep=lambda s: None)
    assert m7._serp_request(Dead().get, "https://x/", lim, "abebooks",
                            sleeps.append) is None
    assert sleeps == [1.0, 2.0]           # max_retries=3 -> 2 backoffs, stop


def test_default_transport_is_honest_and_ipv4():
    assert m7.SCANNER_UA == \
        "lastcopy-scanner/0.1 (bibliographic-preservation-research)"
    assert m7.SCANNER_HEADERS["User-Agent"] == m7.SCANNER_UA
    assert m7.SCANNER_HEADERS["Accept-Encoding"] == "gzip"
    assert m7.MIN_INTERVAL == 2.0 and m7.TIMEOUT == 20


# ---------------------------------------------------------- sightings store
def test_sightings_schema_and_insert(tmp_path):
    conn = m7.connect_out(tmp_path / "sub" / "sightings.db")
    tables = {r["name"] for r in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sightings", "runs"} <= tables
    cols = [r["name"] for r in
            conn.execute("PRAGMA table_info(sightings)")]
    assert cols == ["id", "isbn13", "source", "query_kind", "n_results",
                    "top_price", "currency", "listing_title", "serp_url",
                    "seen_at"]
    r = m7.ScanResult(110, 3.99, "USD", "Pride and Prejudice")
    m7.record_sighting(conn, "9780141439518", "abebooks", r, "isbn",
                       "https://www.abebooks.com/book-search/isbn/"
                       "9780141439518/")
    row = conn.execute("SELECT * FROM sightings").fetchone()
    assert row["source"] == "abebooks" and row["n_results"] == 110
    assert row["top_price"] == pytest.approx(3.99)
    assert row["listing_title"] == "Pride and Prejudice"
    assert row["seen_at"] and row["seen_at"].startswith("20")
    # insert-only: no UPSERT path exists — a second sweep appends a row
    m7.record_sighting(conn, "9780141439518", "abebooks", r, "isbn", "u")
    assert conn.execute("SELECT COUNT(*) c FROM sightings"
                        ).fetchone()["c"] == 2
    conn.close()


# --------------------------------------------------------------- CLI smoke
class PilotGet:
    """Fake transport for the full pilot: robots fixtures + SERP fixtures
    routed by URL; every request recorded."""

    def __init__(self, serps: dict[str, str],
                 robots: dict[str, str] | None = None):
        self.serps = serps
        self.robots = robots or {src: fixture(f"robots_{src}.txt")
                                 for src in m7.DEFAULT_SOURCES}
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        for src, text in self.robots.items():
            if url == m7.SOURCES[src]["robots"]:
                return FakeHTML(text)
        for prefix, body in self.serps.items():
            if url.startswith(prefix):
                return FakeHTML(body)
        return FakeHTML("<html></html>")   # unmatched -> 0-hit parse


class FakeTime:
    """Stand-in for the time module: recording sleep, frozen clock."""

    def __init__(self):
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def test_cli_pilot_smoke_fake_get(tmp_path, capsys, monkeypatch):
    conn = seed_pilot_db(tmp_path)
    conn.close()
    db = tmp_path / "m7.db"
    out = tmp_path / "sightings.db"
    serp = {"https://www.abebooks.com/book-search/":
            fixture("abebooks_isbn.html"),
            "https://www.zvab.com/buch-suchen/": fixture("zvab_isbn.html"),
            "https://www.antiqbook.com/search": fixture("antiqbook_q.html")}
    http = PilotGet(serp)
    fake_time = FakeTime()
    monkeypatch.setattr(m7, "time", fake_time)
    monkeypatch.setattr(m7, "_http_get", http.get)
    rc = m7.main(["--db", str(db), "--out", str(out), "--sample", "10"])
    assert rc == 0
    # robots fetched first for every source
    assert http.urls[:3] == [m7.SOURCES[s]["robots"]
                             for s in m7.DEFAULT_SOURCES]
    # per-book per-source <=1 query; --sample 10 -> quotas 4/2/2/2 capped
    # by availability (3/2/1/2+1 wild books) = 8 books x 3 sources
    n_books = 8
    serp_urls = [u for u in http.urls if "/robots" not in u]
    assert len(serp_urls) == n_books * 3
    # sightings rows inserted with parsed fixture values
    sconn = sqlite3.connect(out)
    sconn.row_factory = sqlite3.Row
    rows = sconn.execute("SELECT * FROM sightings").fetchall()
    assert len(rows) == n_books * 3
    by_src = {s: [r for r in rows if r["source"] == s]
              for s in m7.DEFAULT_SOURCES}
    assert all(r["n_results"] == 111 and r["currency"] == "USD"
               for r in by_src["abebooks"])
    assert all(r["n_results"] == 46 and r["currency"] == "EUR"
               for r in by_src["zvab"])
    assert all(r["n_results"] == 1 for r in by_src["antiqbook"])
    run = sconn.execute("SELECT * FROM runs").fetchone()
    assert run["n_books"] == n_books and run["sources"] == \
        "abebooks,zvab,antiqbook"
    sconn.close()
    out_text = capsys.readouterr().out
    assert "hit-rate" in out_text and "median top_price" in out_text
    # rate discipline: a sleep was requested for nearly every SERP fetch
    # (first request per source needs no wait)
    assert len(fake_time.sleeps) >= len(serp_urls) - 3


def test_cli_pilot_robots_tightened_aborts_source(tmp_path, capsys,
                                                  monkeypatch):
    conn = seed_pilot_db(tmp_path, n_ger=1, n_null=1, n_fre=1, n_und=1,
                         n_other=0, n_not_wild=0)
    conn.close()
    db, out = tmp_path / "m7.db", tmp_path / "sightings.db"
    http = PilotGet({"https://www.abebooks.com/book-search/":
                     fixture("abebooks_isbn.html"),
                     "https://www.zvab.com/buch-suchen/":
                     fixture("zvab_isbn.html"),
                     "https://www.antiqbook.com/search":
                     fixture("antiqbook_q.html")},
                    robots={"abebooks":
                            fixture("robots_abebooks_tightened.txt"),
                            "zvab": fixture("robots_zvab.txt")})
    monkeypatch.setattr(m7, "_http_get", http.get)
    monkeypatch.setattr(m7, "time", FakeTime())
    rc = m7.main(["--db", str(db), "--out", str(out), "--sample", "4",
                  "--sources", "abebooks,zvab"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ABORT source abebooks" in err and "TIGHTENED" in err
    assert not any("abebooks.com/book-search" in u for u in http.urls)
    sconn = sqlite3.connect(out)
    sources = {r[0] for r in sconn.execute("SELECT DISTINCT source "
                                           "FROM sightings")}
    assert sources == {"zvab"}
    sconn.close()


def test_cli_pilot_rejects_unknown_source(tmp_path):
    with pytest.raises(SystemExit) as exc:
        m7.main(["--db", str(tmp_path / "x.db"), "--sources", "ebay"])
    assert exc.value.code == 2


# ------------------------------------------------- language inference (M7+)
def test_infer_language_clear_cases():
    assert m7.infer_language(
        "The History of the English People, Complete") == "eng"
    assert m7.infer_language(
        "Die Geschichte der Stadt und ihrer Bewohner") == "ger"
    assert m7.infer_language(
        "Histoire de la vie du peuple dans les villes") == "fre"


def test_infer_language_ambiguous_and_null():
    # one marker each side: no language reaches 2 -> None
    assert m7.infer_language("Complete des") is None
    # no markers at all -> None
    assert m7.infer_language("Opuscula Quaedam") is None
    # NULL/empty title -> None
    assert m7.infer_language(None) is None
    assert m7.infer_language("") is None


def test_infer_language_negative_dominance_blocks_eng():
    # >=2 eng markers but German function words dominate -> NOT eng
    assert m7.infer_language(
        "The Life of der und von dem und zur Zeit") == "ger"
    # tie at >=2 with no clean winner -> 'other'
    assert m7.infer_language("The History le sur") == "other"


# ------------------------------------------------------ scanner-english-set
def test_cli_english_set_counts_and_csv(tmp_path, capsys):
    conn = seed_pilot_db(tmp_path, n_ger=0, n_null=2, n_fre=1, n_und=3,
                         n_other=0, n_not_wild=1)
    # insertion order: 1-2 NULL, 3 fre, 4-6 und, 7 not-wild
    def set_book(i, title, lang):
        conn.execute("UPDATE candidates SET title=?, language=? "
                     "WHERE isbn13=?", (title, lang, isbn13_for(i)))
    set_book(1, "The History of the English Church", None)   # infers eng
    set_book(2, "Kleines Worterbuch", None)                  # None -> out
    set_book(3, "La vie des poissons", "fre")                # lang filter
    set_book(4, "Letters and Life of Robert Browning", "und")  # infers eng
    set_book(5, "Die Geschichte der Alchemie", "und")        # infers ger
    set_book(6, "Opuscula Quaedam", "eng")                   # MARC eng
    set_book(7, "The Complete Poems and Letters", None)      # NOT wild
    conn.commit()
    conn.close()
    out = tmp_path / "english_wild.csv"
    rc = m7.main_english_set(["--db", str(tmp_path / "m7.db"),
                              "--out", str(out)])
    assert rc == 0
    import csv
    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["isbn13"] for r in rows] == [isbn13_for(1), isbn13_for(4),
                                           isbn13_for(6)]
    assert rows[0]["lang_inferred"] == "eng"
    assert rows[0]["language_code"] == ""        # NULL language -> empty
    assert rows[1]["language_code"] == "und"
    assert rows[2]["language_code"] == "eng"     # MARC eng, inferred ""
    assert rows[2]["lang_inferred"] == ""
    assert rows[0]["title"] == "The History of the English Church"
    stdout = capsys.readouterr().out
    assert "3 rows" in stdout
    assert "2 title-inferred eng" in stdout
    assert "1 MARC eng" in stdout


# --------------------------------------------------------- --isbn-file path
def test_cli_pilot_isbn_file_bypasses_sampler(tmp_path, monkeypatch):
    conn = seed_pilot_db(tmp_path)   # 9 wild + 2 not-wild
    conn.close()
    db, out = tmp_path / "m7.db", tmp_path / "sightings.db"
    # listed: wild ger(1), wild other(9), NOT-wild(10) — sampler filters
    # would never take 10 — plus an unknown ISBN (absent from the DB)
    listed = [isbn13_for(1), isbn13_for(9), isbn13_for(10),
              isbn13_for(99)]
    isbn_file = tmp_path / "isbns.txt"
    isbn_file.write_text(
        "# explicit sweep list\n" + "\n".join(listed) + "\n\n",
        encoding="utf-8")
    serp = {"https://www.abebooks.com/book-search/":
            fixture("abebooks_isbn.html"),
            "https://www.zvab.com/buch-suchen/": fixture("zvab_isbn.html"),
            "https://www.antiqbook.com/search": fixture("antiqbook_q.html")}
    http = PilotGet(serp)
    monkeypatch.setattr(m7, "_http_get", http.get)
    monkeypatch.setattr(m7, "time", FakeTime())
    rc = m7.main(["--db", str(db), "--out", str(out),
                  "--isbn-file", str(isbn_file)])
    assert rc == 0
    wanted = set(listed[:3])            # unknown ISBN(99) never queried
    sconn = sqlite3.connect(out)
    sconn.row_factory = sqlite3.Row
    seen = {r["isbn13"] for r in
            sconn.execute("SELECT isbn13 FROM sightings")}
    assert seen == wanted               # exactly the listed, in-DB ISBNs
    run = sconn.execute("SELECT * FROM runs").fetchone()
    assert run["n_books"] == 3
    sconn.close()
    serp_urls = [u for u in http.urls if "/robots" not in u]
    assert len(serp_urls) == 3 * 3      # 3 books x 3 sources, nothing else


# ------------------------------------------------------ circulation tiers
def test_circulation_tier_boundaries():
    ct = m7.circulation_tier
    # 0 / 'none' -> wild (unknown offerings behave like zero)
    assert ct(0, None) == "wild"
    assert ct(None, None) == "wild"
    # 1-2 -> mild (price irrelevant at this scarcity)
    assert ct(1, None) == "mild"
    assert ct(2, 100.0) == "mild"
    # 3-5 -> steady
    assert ct(3, None) == "steady"
    assert ct(5, 1.0) == "steady"
    # >=6 AND known cheap min price -> common-in-trade
    assert ct(6, 5.0) == "common-in-trade"
    assert ct(6, 14.99) == "common-in-trade"
    assert ct(600, 0.01) == "common-in-trade"
    # >=6 otherwise -> steady+ (expensive, or price unknown)
    assert ct(6, 15.0) == "steady+"     # 15.0 is NOT < 15.0
    assert ct(6, None) == "steady+"     # 6 expensive / unknown-price
    assert ct(100, 50.0) == "steady+"


def _sight(i13: str, source: str, n: int, price: float | None,
           currency: str = "USD") -> None:
    m7.record_sighting(_sight.conn, i13, source,
                       m7.ScanResult(n, price, currency if n else None,
                                     None),
                       "isbn", f"https://x/{i13}/{source}")


def test_market_aggregates_latest_per_source(tmp_path):
    conn = m7.connect_out(tmp_path / "sightings.db")
    _sight.conn = conn
    a, b = isbn13_for(1), isbn13_for(2)
    _sight(a, "abebooks", 10, 4.0)          # book a: 10 + 2, min 3.0
    _sight(a, "zvab", 2, 3.0)
    _sight(b, "abebooks", 0, None)          # book b: 0 offerings
    _sight(a, "abebooks", 7, 12.0)          # repeat sweep: latest wins
    agg = m7.market_aggregates(conn)
    assert agg[a]["offerings"] == 9         # 7 (latest) + 2, not 10+2+7
    assert min(agg[a]["prices"]) == pytest.approx(3.0)
    assert agg[b]["offerings"] == 0
    conn.close()


# ------------------------------------------------------ scanner-calibration
def test_cli_calibration_csv_shape(tmp_path, capsys):
    mconn = seed_pilot_db(tmp_path)         # titles via m4 ATTACH
    title1 = mconn.execute(
        "SELECT title FROM candidates WHERE isbn13=?",
        (isbn13_for(1),)).fetchone()["title"]
    mconn.close()
    sdb = tmp_path / "sightings.db"
    conn = m7.connect_out(sdb)
    _sight.conn = conn
    _sight(isbn13_for(1), "abebooks", 10, 4.0)
    _sight(isbn13_for(1), "zvab", 2, 3.0)
    _sight(isbn13_for(2), "abebooks", 0, None)
    conn.close()
    out = tmp_path / "calibration_sample.csv"
    rc = m7.main_calibration(["--db", str(sdb), "--m4",
                              str(tmp_path / "m7.db"), "--n", "30",
                              "--out", str(out)])
    assert rc == 0
    import csv
    with open(out, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        assert reader.fieldnames == ["isbn13", "title", "offerings_est",
                                     "link_abebooks", "link_zvab",
                                     "link_antiqbook"]
    assert len(rows) == 2                   # both sighted books sampled
    by_isbn = {r["isbn13"]: r for r in rows}
    r1 = by_isbn[isbn13_for(1)]
    assert r1["title"] == title1            # resolved via m4 ATTACH
    assert r1["offerings_est"] == "12"      # summed latest-per-source
    assert r1["link_abebooks"] == (
        f"https://www.abebooks.com/book-search/isbn/{isbn13_for(1)}/")
    assert r1["link_zvab"].endswith(f"/isbn/{isbn13_for(1)}/")
    assert f"q={isbn13_for(1)}" in r1["link_antiqbook"]
    assert by_isbn[isbn13_for(2)]["offerings_est"] == "0"
    assert "2 books" in capsys.readouterr().out


def test_cli_calibration_n_limits_sample(tmp_path):
    mconn = seed_pilot_db(tmp_path)
    mconn.close()
    sdb = tmp_path / "sightings.db"
    conn = m7.connect_out(sdb)
    _sight.conn = conn
    for i in range(1, 5):
        _sight(isbn13_for(i), "abebooks", i, 2.0)
    conn.close()
    out = tmp_path / "cal.csv"
    rc = m7.main_calibration(["--db", str(sdb), "--m4",
                              str(tmp_path / "m7.db"), "--n", "3",
                              "--out", str(out)])
    assert rc == 0
    import csv
    with open(out, newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 3   # random subset of 4


# --------------------------------------------------------- findings.py CSV
def _load_findings():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "findings", Path(__file__).resolve().parent.parent / "scripts"
        / "findings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_findings_csv_risk_tier_after_currency(tmp_path):
    findings = _load_findings()
    mconn = seed_pilot_db(tmp_path)
    mconn.close()
    sdb = tmp_path / "sightings.db"
    conn = m7.connect_out(sdb)
    _sight.conn = conn
    a, b = isbn13_for(1), isbn13_for(2)
    _sight(a, "abebooks", 10, 4.0)          # 12 offerings, min 3.0
    _sight(a, "zvab", 2, 3.0, currency="EUR")
    _sight(b, "abebooks", 0, None)          # 0 offerings -> wild
    conn.close()
    out = tmp_path / "findings.csv"
    n = findings.write_findings(sdb, tmp_path / "m7.db", out)
    assert n == 3                            # per-sighting rows
    import csv
    with open(out, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        assert reader.fieldnames[:9] == [
            "isbn13", "title", "year", "score", "source", "n_results",
            "top_price", "currency", "risk_tier"]      # after currency
    tiers = {r["isbn13"]: r["risk_tier"] for r in rows}
    assert tiers[a] == "common-in-trade"     # 12 offerings, min 3.0 < 15
    assert tiers[b] == "wild"
    # titles resolved via the m4 ATTACH
    assert all(r["title"].startswith("Wild Book") for r in rows)
