"""M7 — marketplace scanner PILOT: 500-book stratified sample vs 3
verified sources (SPEC-M7-SCANNER-PILOT).

Sources (research/2026-10-06-scanner-sources.md, all live-verified):
  abebooks  /book-search/isbn/<i>/  + /book-search/title/<t>/author/<a>/
  zvab      /buch-suchen/isbn/<i>/  + /buch-suchen/titel/<t>/autor/<a>/
  antiqbook /search?q=<isbn or title surname>

COMPLIANCE (non-negotiable):
  - robots.txt re-fetched and diffed at run start; ANY tightening of the
    ruling that keeps our paths open -> that source is ABORTED with a
    clear message and never queried.
  - SERPs are parsed ONLY.  AbeBooks/ZVAB result links point at
    /servlet/ (robots-DISALLOWED) and are NEVER followed.
  - Honest User-Agent: lastcopy-scanner/0.1 (bibliographic-preservation-research)
  - <=0.5 req/s per source (min 2.0 s between requests per source; the
    round-robin interleave keeps the per-source rate while 3 sources
    give a global ~1.5 rps ceiling that stays well under politeness norms).

Sightings are INSERT-ONLY into data/scanner/sightings.db (trend lines
come from repeat sweeps, never from updates).
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import m4, m5, m6

SCANNER_UA = "lastcopy-scanner/0.1 (bibliographic-preservation-research)"
SCANNER_HEADERS = {"User-Agent": SCANNER_UA, "Accept-Encoding": "gzip"}
MIN_INTERVAL = 2.0            # s between requests PER SOURCE (<=0.5 req/s)
MAX_RETRIES = 3               # exponential backoff, _sru_request discipline
TIMEOUT = 20                  # s per SERP request
PROGRESS_EVERY = 25           # books between live progress lines
DEFAULT_SOURCES = ("abebooks", "zvab", "antiqbook")

# pilot strata (postmortem 2026-10-06: the wild-CR population uses MARC
# language codes — ger 17,635 / NULL 12,465 / fre 3,270 / und 2,451 /
# ~0 eng): n=500 -> ger 200 / NULL 100 / fre 100 / und+other 100, with
# und getting first shot at the und+other quota.
STRATA = (("ger", 200), ("null", 100), ("fre", 100), ("und_other", 100))
_PILOT_TOTAL = sum(q for _, q in STRATA)


class StratumShortfallError(RuntimeError):
    """A sampler stratum returned < 50% of its quota (postmortem item 2:
    the pilot's silent collapse to the top-50 masked three empty strata).
    Carries the per-stratum shortfall table in the message; fail LOUD."""


@dataclass
class Book:
    isbn13: str | None
    title: str | None
    author: str | None
    year: int | None
    language: str | None
    score: int


@dataclass
class ScanResult:
    n_results: int
    top_price: float | None
    currency: str | None
    listing_title: str | None


# ------------------------------------------------------------------ queries
def slug(s: str | None) -> str:
    """URL segment: ASCII-fold, lowercase, non-alnum -> '-', collapsed."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def _author_seg(author: str | None) -> str:
    return slug(m6.author_surname(author))


SOURCES = {
    "abebooks": {
        "base": "https://www.abebooks.com",
        "robots": "https://www.abebooks.com/robots.txt",
        "isbn": lambda b: f"https://www.abebooks.com/book-search/isbn/{b}/",
        "title": lambda t, a: (f"https://www.abebooks.com/book-search/title/"
                               f"{slug(t)}/author/{_author_seg(a)}/"),
    },
    "zvab": {
        "base": "https://www.zvab.com",
        "robots": "https://www.zvab.com/robots.txt",
        "isbn": lambda b: f"https://www.zvab.com/buch-suchen/isbn/{b}/",
        "title": lambda t, a: (f"https://www.zvab.com/buch-suchen/titel/"
                               f"{slug(t)}/autor/{_author_seg(a)}/"),
    },
    "antiqbook": {
        "base": "https://www.antiqbook.com",
        "robots": "https://www.antiqbook.com/robots.txt",
        "isbn": lambda b: f"https://www.antiqbook.com/search?q={b}",
        "title": lambda t, a: ("https://www.antiqbook.com/search?q="
                               + urllib.parse.quote(f"{t} {m6.author_surname(a)}")),
    },
}


def build_query(source: str, book: Book) -> tuple[str, str]:
    """(url, query_kind) — ISBN form when the book carries an ISBN13,
    else title/surname form.  Per-book per-source exactly one query."""
    spec = SOURCES[source]
    if book.isbn13:
        return spec["isbn"](book.isbn13), "isbn"
    return spec["title"](book.title or "", book.author or ""), "title"


# ------------------------------------------------------------------ sampler
_WILD_WHERE = ("s.status='CR' AND (s.custody_physical IS NULL "
               "OR s.custody_physical='wild') "
               "AND (s.holdings IS NULL OR s.holdings='')")


def build_pilot_sample(conn: sqlite3.Connection, n: int = 500) -> list[Book]:
    """Stratified sample of wild books (status CR, no holdings): ger /
    NULL / fre / und+other (und first, then any other language tops the
    quota up), ORDER BY score DESC (isbn13 ASC tiebreak) within each
    stratum.  Strata quotas scale with n (500 -> the SPEC quotas).

    FAIL-LOUD guard: any stratum returning < 50% of its quota raises
    StratumShortfallError with the per-stratum shortfall table."""
    m5.ensure_schema(conn)
    scale = n / _PILOT_TOTAL

    def fetch(where: str, q: int) -> list:
        return conn.execute(
            f"""SELECT w.isbn13, c.title, c.year, c.language, w.score,
                       wr.author_keys
                FROM enrich_status s
                JOIN enrich_workset w ON w.isbn13 = s.isbn13
                LEFT JOIN candidates c ON c.isbn13 = w.isbn13
                LEFT JOIN works_ref wr ON wr.work_key = w.work_key
                WHERE {_WILD_WHERE} AND {where}
                ORDER BY w.score DESC, w.isbn13 ASC LIMIT ?""",
            (q,)).fetchall()

    books: list[Book] = []
    shortfalls: list[tuple[str, int, int]] = []
    for lang, quota in STRATA:
        q = max(1, round(quota * scale)) if n >= 1 else 0   # >=1 per stratum
        if q == 0:
            continue
        if lang == "und_other":      # und first, misc languages top up
            rows = fetch("c.language='und'", q)
            if len(rows) < q:
                rows += fetch("(c.language IS NOT NULL AND c.language "
                              "NOT IN ('ger','fre','und'))", q - len(rows))
        else:
            where = ("c.language='ger'" if lang == "ger" else
                     "c.language IS NULL" if lang == "null" else
                     "c.language='fre'")
            rows = fetch(where, q)
        for r in rows:
            books.append(_row_to_book(conn, r))
        if len(rows) * 2 < q:
            shortfalls.append((lang, len(rows), q))
    if shortfalls:
        table = "\n".join(
            f"  {lang}: got {got}/{quota_want} (<50% of quota)"
            for lang, got, quota_want in shortfalls)
        raise StratumShortfallError(
            "pilot sampler stratum shortfall (population drifted from "
            f"the postmortem counts):\n{table}")
    return books


def _row_to_book(conn: sqlite3.Connection, r) -> Book:
    return Book(isbn13=r["isbn13"], title=r["title"],
                author=m4.resolve_authors(conn, r["author_keys"]),
                year=r["year"], language=r["language"] or "null",
                score=r["score"] or 0)


_BOOK_SELECT = """SELECT w.isbn13, c.title, c.year, c.language, w.score,
                          wr.author_keys
                   FROM enrich_workset w
                   LEFT JOIN candidates c ON c.isbn13 = w.isbn13
                   LEFT JOIN works_ref wr ON wr.work_key = w.work_key
                   WHERE w.isbn13=?"""


def books_for_isbns(conn: sqlite3.Connection,
                    isbns: list[str]) -> list[Book]:
    """Explicit ISBN list -> Book rows (sampler BYPASSED entirely: no
    wild-CR filter, no strata, no quotas — the caller listed exactly
    what to sweep).  ISBNs unknown to the DB are silently absent."""
    books: list[Book] = []
    for i13 in isbns:
        r = conn.execute(_BOOK_SELECT, (i13,)).fetchone()
        if r is not None:
            books.append(_row_to_book(conn, r))
    return books


def read_isbn_file(path: str | Path) -> list[str]:
    """One ISBN (13) per line; blanks and '#' comments skipped."""
    out: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


# ------------------------------------------------- english-set (theory dir.)
def english_set_rows(conn: sqlite3.Connection) -> list[dict]:
    """Wild-CR books whose MARC language is eng/NULL/und/empty, with the
    title-language inference attached (pure read — nothing is stored).
    Rows where inference says 'eng' OR the MARC code already says 'eng'
    are the English-first sweep set."""
    m5.ensure_schema(conn)
    rows = conn.execute(
        f"""SELECT w.isbn13, c.title, c.year, c.language, w.score,
                   wr.author_keys
            FROM enrich_status s
            JOIN enrich_workset w ON w.isbn13 = s.isbn13
            LEFT JOIN candidates c ON c.isbn13 = w.isbn13
            LEFT JOIN works_ref wr ON wr.work_key = w.work_key
            WHERE {_WILD_WHERE}
              AND (c.language IS NULL OR c.language=''
                   OR c.language IN ('eng','und'))
            ORDER BY w.score DESC, w.isbn13 ASC""").fetchall()
    out: list[dict] = []
    for r in rows:
        lang_code = r["language"] or ""
        inferred = infer_language(r["title"])
        if inferred == "eng" or lang_code == "eng":
            out.append({"isbn13": r["isbn13"], "title": r["title"],
                        "author": m4.resolve_authors(conn, r["author_keys"]),
                        "year": r["year"], "language_code": lang_code,
                        "lang_inferred": inferred or ""})
    return out


def write_english_csv(rows: list[dict], out: str | Path) -> None:
    import csv
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=("isbn13", "title", "author",
                                           "year", "language_code",
                                           "lang_inferred"))
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------ language inference
# Theory directive (English-first sweep): a pure title classifier — no
# state in m4.db.  English markers are common function/content words of
# English-language book titling; the negative markers are the equivalent
# German/French function words.  eng requires >=2 markers AND no
# negative-language dominance (ger/fre symmetric); anything else -> None.
ENGLISH_MARKERS = frozenset((
    "the", "of", "and", "history", "story", "life", "letters", "works",
    "poems", "manual", "introduction", "studies", "principles", "tales",
    "adventures", "memoir", "memoirs", "selected", "complete", "practical",
    "elementary", "essays", "novel", "guide", "handbook", "course",
    "lectures", "treatise", "methods", "theory", "practice", "text",
    "reader", "collection", "volume", "edition", "being", "study",
    "nature", "england", "english", "american", "london"))
GERMAN_MARKERS = frozenset((
    "der", "die", "das", "und", "von", "mit", "fur", "zur", "auf", "den",
    "dem", "ein", "eine", "geschichte"))
FRENCH_MARKERS = frozenset((
    "le", "la", "les", "des", "du", "une", "dans", "pour", "sur"))


def _title_tokens(title: str | None) -> list[str]:
    """ASCII-folded, lowercased word tokens of a title."""
    if not title:
        return []
    s = unicodedata.normalize("NFKD", str(title))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"[a-z]+", s.lower())


def infer_language(title: str | None) -> str | None:
    """'eng' | 'ger' | 'fre' | 'other' | None from title words alone.

    eng: >=2 English markers and strictly more than either negative-
    language count (no negative dominance); ger/fre symmetric.  'other'
    when some language reaches >=2 markers but no clean winner emerges
    (tie); None when nothing reaches 2 markers (or the title is NULL)."""
    tokens = _title_tokens(title)
    if not tokens:
        return None
    n_eng = sum(t in ENGLISH_MARKERS for t in tokens)
    n_ger = sum(t in GERMAN_MARKERS for t in tokens)
    n_fre = sum(t in FRENCH_MARKERS for t in tokens)
    for code, n, n_a, n_b in (("eng", n_eng, n_ger, n_fre),
                              ("ger", n_ger, n_eng, n_fre),
                              ("fre", n_fre, n_eng, n_ger)):
        if n >= 2 and n > n_a and n > n_b:
            return code
    if max(n_eng, n_ger, n_fre) >= 2:
        return "other"
    return None


# ------------------------------------------------------------- rate limiter
class RateLimiter:
    """Min-interval pacing PER SOURCE.  ``clock``/``sleep`` injectable for
    tests (fake sleep); one process interleaves sources round-robin so the
    per-source gap holds regardless of interleaving."""

    def __init__(self, min_interval: float = MIN_INTERVAL,
                 clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last: dict[str, float] = {}

    def wait(self, source: str) -> None:
        now = self.clock()
        last = self._last.get(source)
        if last is not None:
            delay = self.min_interval - (now - last)
            if delay > 0:
                self.sleep(delay)
        self._last[source] = self.clock()


# ------------------------------------------------------------------ fetcher
def _http_get(url: str):
    """Default (real) transport: httpx sync, IPv4-forced (same estate
    discipline as m5/m6), honest scanner UA, 20 s timeout, gzip."""
    import httpx
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      transport=transport) as client:
        return client.get(url, headers=SCANNER_HEADERS)


def _serp_request(get, url: str, limiter: RateLimiter, source: str,
                  sleep, max_retries: int = MAX_RETRIES):
    """SERP GET with _sru_request discipline: per-source min-interval
    pacing, exponential backoff on 429/503 AND transport errors, then
    None (failed query is simply not recorded; the sweep retries next run)."""
    import httpx
    attempt = 0
    while True:
        limiter.wait(source)
        try:
            resp = get(url)
        except httpx.HTTPError:
            if attempt < max_retries - 1:
                sleep(2.0 ** attempt)
                attempt += 1
                continue
            return None
        if resp.status_code in (429, 503) and attempt < max_retries - 1:
            sleep(2.0 ** attempt)
            attempt += 1
            continue
        if getattr(resp, "ok", 200 <= resp.status_code < 300):
            return resp.text
        return None


# ------------------------------------------------------------------ parsers
# AbeBooks live SERP (probe 2026-10-06, postmortem item 3): the embedded
# '"totalResults": N' app-state is GONE; the count now renders as
# <span data-test-id="result-count"> (N results)</span> and listings are
# schema.org JSON-LD items (name / price / priceCurrency).
_ABE_COUNT_RE = re.compile(
    r'data-test-id="result-count">\s*\(\s*(\d+)\s+results?\s*\)')
_PRICE_JSON_RE = re.compile(r'"price":\s*([0-9]+(?:[.,][0-9]+)?)')
_CURRENCY_RE = re.compile(r'"priceCurrency":\s*"([A-Z]{3})"')
_JSONLD_NAME_RE = re.compile(r'"@type":"Book",\s*"name":\s*"([^"]{1,200}?)"')
_JSON_TITLE_RE = re.compile(r'"title":\s*"([^"]{1,200}?)"')
_ERGEBNISSE_RE = re.compile(r'(\d+)\s+Ergebnisse')
# ZVAB price forms (postmortem item 4): pages now render 'EUR\u00a020'
# (nbsp) as well as the older '€ 20'; accept EUR/€ with nbsp or space
# (or none) before the amount.
_EURO_PRICE_RE = re.compile(
    r'(?:€|EUR)[ \u00a0]*([0-9]+(?:[.,][0-9]{1,2})?)')
_ANTIQ_COUNT_RE = re.compile(r'(\d+)\s+antiquarian books')
_ANTIQ_LINK_RE = re.compile(
    r'<a[^>]+href="/book/[0-9]+/[^"]*"[^>]*>([^<]{1,200})</a>')


def _min_price(values: list[str]) -> float | None:
    if not values:
        return None
    return min(float(v.replace(",", ".")) for v in values)


def parse_abebooks(text: str) -> ScanResult:
    """Live SERP (probe 2026-10-06): result-count span marker + first-page
    schema.org JSON-LD listing prices.  Marker absent -> 0-hit (SPEC M7
    item 4)."""
    m = _ABE_COUNT_RE.search(text)
    if not m:
        return ScanResult(0, None, None, None)
    n = int(m.group(1))
    cur = _CURRENCY_RE.search(text)
    title = _JSONLD_NAME_RE.search(text)
    return ScanResult(n, _min_price(_PRICE_JSON_RE.findall(text)),
                      cur.group(1) if cur else "USD",
                      title.group(1) if title else None)


def parse_zvab(text: str) -> ScanResult:
    """German SERP: 'N Ergebnisse' + € prices on the first page."""
    m = _ERGEBNISSE_RE.search(text)
    n = int(m.group(1)) if m else 0
    title = _JSON_TITLE_RE.search(text)
    return ScanResult(n, _min_price(_EURO_PRICE_RE.findall(text)),
                      "EUR" if n else None,
                      title.group(1) if title else None)


def parse_antiqbook(text: str) -> ScanResult:
    """Lean server HTML: 'N antiquarian books' + /book/<id>/<slug> result
    links; first-page prices are EUR."""
    m = _ANTIQ_COUNT_RE.search(text)
    n = int(m.group(1)) if m else 0
    title = _ANTIQ_LINK_RE.search(text)
    return ScanResult(n, _min_price(_EURO_PRICE_RE.findall(text)),
                      "EUR" if n else None,
                      title.group(1).strip() if title else None)


PARSERS = {"abebooks": parse_abebooks, "zvab": parse_zvab,
           "antiqbook": parse_antiqbook}


# ------------------------------------------------------ circulation tiers
# APPROVED schema (Theory 2026-10-09: "seems like a good schema").  Pure
# function; tiers influence NO scoring until the calibration hand-check
# (scanner-calibration) has verified the counts against live SERPs.
def circulation_tier(offerings: int | None, min_price: float | None) -> str:
    """'wild' (0/no offerings) | 'mild' (1-2) | 'steady' (3-5) |
    'common-in-trade' (>=6 AND a known min price < 15.0 — many cheap
    listings) | 'steady+' (>=6 but expensive or price unknown: many
    listings, yet none of them cheap)."""
    n = offerings or 0
    if n <= 0:
        return "wild"
    if n <= 2:
        return "mild"
    if n <= 5:
        return "steady"
    if min_price is not None and min_price < 15.0:
        return "common-in-trade"
    return "steady+"


def market_aggregates(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-book market view over the sightings store: ``offerings`` =
    summed n_results of the LATEST sighting per source (insert-only rows,
    so MAX(id) per (isbn13, source) group is the most recent sweep —
    repeat sweeps never double-count) and ``min_price`` = cheapest
    top_price among those latest rows.  Input to circulation_tier and to
    the calibration sample."""
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT isbn13, source, n_results, top_price FROM sightings "
            "WHERE isbn13 IS NOT NULL AND id IN (SELECT MAX(id) FROM "
            "sightings WHERE isbn13 IS NOT NULL GROUP BY isbn13, source)"):
        d = out.setdefault(r["isbn13"], {"offerings": 0, "prices": []})
        d["offerings"] += r["n_results"] or 0
        if r["top_price"] is not None:
            d["prices"].append(r["top_price"])
    return out


# --------------------------------------------------------------- robots guard
def _robots_star_disallows(text: str) -> list[str]:
    """Disallow paths from the User-agent: * group(s) (empty value = allow)."""
    disallows: list[str] = []
    in_star = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            in_star = value == "*"
        elif field == "disallow" and in_star and value:
            disallows.append(value)
    return disallows


def check_robots(sources, get) -> dict[str, dict]:
    """Fetch every source's robots.txt at run start and compare the
    ``*`` ruling against expectations (our query paths stay allowed).
    ANY tightening — a Disallow rule that now covers one of our paths —
    aborts that source with a clear message; it is not queried."""
    verdicts: dict[str, dict] = {}
    for src in sources:
        spec = SOURCES[src]
        probe = Book(isbn13="9780141439518", title="x", author="x", year=None,
                     language=None, score=0)
        path = urllib.parse.urlparse(build_query(src, probe)[0]).path
        try:
            resp = get(spec["robots"])
            ok = getattr(resp, "ok", 200 <= resp.status_code < 300)
        except Exception as exc:            # robots unreadable -> do not run
            verdicts[src] = {"ok": False,
                             "reason": f"robots fetch failed: {exc!r}"}
            continue
        if not ok:
            # RFC 9309: an Unavailable robots.txt (4xx) means the crawler
            # MUST NOT access any pages (the Alibris precedent, research
            # 2026-10-06) — abort the source.
            verdicts[src] = {
                "ok": False,
                "reason": (f"robots.txt UNAVAILABLE (HTTP "
                           f"{resp.status_code}) — entire site treated as "
                           f"disallowed; source ABORTED")}
            continue
        blocking = [d for d in _robots_star_disallows(resp.text)
                    if path.startswith(d.rstrip("*"))]
        if blocking:
            verdicts[src] = {
                "ok": False,
                "reason": (f"robots.txt TIGHTENED: 'Disallow: {blocking[0]}' "
                           f"now covers our query path {path} — source "
                           f"ABORTED (compliance guard, SPEC M7)")}
        else:
            verdicts[src] = {"ok": True, "reason": f"{path} open for '*'"}
    return verdicts


# ------------------------------------------------------------ sightings store
SIGHTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
  id INTEGER PRIMARY KEY,
  isbn13 TEXT,
  source TEXT,
  query_kind TEXT,
  n_results INTEGER,
  top_price REAL,
  currency TEXT,
  listing_title TEXT,
  serp_url TEXT,
  seen_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT,
  n_books INT,
  sources TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_sightings_isbn ON sightings(isbn13, source);
CREATE INDEX IF NOT EXISTS idx_sightings_source ON sightings(source, seen_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_out(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SIGHTINGS_SCHEMA)
    conn.commit()
    return conn


def record_sighting(conn: sqlite3.Connection, isbn13: str | None,
                    source: str, result: ScanResult, query_kind: str,
                    serp_url: str) -> None:
    """Insert-only (SPEC M7 item 6): trend lines come from repeat sweeps."""
    conn.execute(
        "INSERT INTO sightings (isbn13, source, query_kind, n_results, "
        "top_price, currency, listing_title, serp_url, seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (isbn13, source, query_kind, result.n_results, result.top_price,
         result.currency, result.listing_title, serp_url, _now()))
    conn.commit()


# ------------------------------------------------------------------- pilot
def run_pilot(conn: sqlite3.Connection, out: str | Path,
              sample: int = 500, sources=DEFAULT_SOURCES, *,
              get=None, sleep=None, limiter: RateLimiter | None = None,
              max_seconds: float | None = None,
              progress_every: int = PROGRESS_EVERY,
              books: list[Book] | None = None) -> dict:
    """The pilot sweep: stratified sample (or an explicit ``books`` list
    — --isbn-file bypasses the sampler entirely) -> round-robin
    per-source queries (<=1 query per book per source) -> insert-only
    sightings.  Robots is checked FIRST; tightened sources are aborted
    and skipped."""
    if get is None:
        get = _http_get
    if sleep is None:
        sleep = time.sleep     # resolved at call time (test patch point)
    if limiter is None:
        limiter = RateLimiter(sleep=sleep)
    if books is None:
        books = build_pilot_sample(conn, sample)
    verdicts = check_robots(sources, get)
    active = [s for s in sources if verdicts[s]["ok"]]
    for src, v in verdicts.items():
        if not v["ok"]:
            print(f"[scanner] ABORT source {src}: {v['reason']}",
                  file=sys.stderr, flush=True)
    out_conn = connect_out(out)
    aborted = [s for s in sources if s not in active]
    cur = out_conn.execute(
        "INSERT INTO runs (started_at, n_books, sources, notes) "
        "VALUES (?,?,?,?)",
        (_now(), len(books), ",".join(active),
         f"aborted: {','.join(aborted)}" if aborted else None))
    run_row = cur.lastrowid
    out_conn.commit()
    deadline = time.monotonic() + max_seconds if max_seconds else None
    n_queries = 0
    for i, book in enumerate(books, 1):
        for src in active:
            if deadline and time.monotonic() >= deadline:
                print(f"[scanner] max-seconds {max_seconds} reached at book "
                      f"{i-1}/{len(books)} — stopping sweep",
                      file=sys.stderr, flush=True)
                return _summarize(out_conn, active, run_row, books, i - 1)
            url, kind = build_query(src, book)
            text = _serp_request(get, url, limiter, src, sleep)
            if text is None:
                continue        # failed query, not recorded; retried next sweep
            record_sighting(out_conn, book.isbn13, src, PARSERS[src](text),
                            kind, url)
            n_queries += 1
        if progress_every and i % progress_every == 0:
            print(f"[scanner] {i}/{len(books)} books "
                  f"({n_queries} queries)", file=sys.stderr, flush=True)
    return _summarize(out_conn, active, run_row, books, len(books))


def _summarize(out_conn, active, run_row, books, done_books) -> dict:
    """Final summary: hit-rate per source x language bucket + median
    top_price per source, from the rows this run wrote."""
    summary: dict = {"run": run_row, "books": len(books),
                     "books_done": done_books, "sources": active,
                     "per_source": {}}
    prices_by_src: dict[str, list[float]] = {s: [] for s in active}
    for src in active:
        rows = out_conn.execute(
            "SELECT n_results, top_price, isbn13 FROM sightings "
            "WHERE source=? AND seen_at >= (SELECT started_at FROM runs "
            "WHERE id=?)", (src, run_row)).fetchall()
        seen = {r["isbn13"] for r in rows}
        hit_rows = [r for r in rows if r["n_results"] and r["n_results"] > 0]
        hit_isbns = {r["isbn13"] for r in hit_rows}
        for r in hit_rows:
            if r["top_price"] is not None:
                prices_by_src[src].append(r["top_price"])
        by_lang: dict[str, dict] = {}
        for b in books[:done_books]:
            if b.isbn13 not in seen:
                continue
            lang = b.language if b.language in ("ger", "fre", "und") \
                else ("null" if b.language in (None, "null") else "other")
            d = by_lang.setdefault(lang, {"queried": 0, "hits": 0})
            d["queried"] += 1
            if b.isbn13 in hit_isbns:
                d["hits"] += 1
        summary["per_source"][src] = {
            "queries": len(rows),
            "hit_rate": (len(hit_rows) / len(rows)) if rows else 0.0,
            "median_top_price": (statistics.median(prices_by_src[src])
                                 if prices_by_src[src] else None),
            "by_language": {k: (v["hits"] / v["queried"] if v["queried"] else 0.0)
                            for k, v in sorted(by_lang.items())}}
    out_conn.close()
    for src, s in summary["per_source"].items():
        med = (f"{s['median_top_price']:.2f}"
               if s["median_top_price"] is not None else "n/a")
        langs = ", ".join(f"{k}={v:.0%}" for k, v in s["by_language"].items())
        print(f"[scanner] {src}: {s['queries']} queries, "
              f"hit-rate {s['hit_rate']:.0%}, median top_price {med}"
              + (f" ({langs})" if langs else ""))
    return summary


# --------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="scanner-pilot",
        description="M7 marketplace scanner pilot: stratified wild-book "
                    "sample vs robots-verified SERPs (abebooks, zvab, "
                    "antiqbook); insert-only sightings DB.")
    p.add_argument("--db", default="lastcopy.db", help="M4/M5 SQLite DB path")
    p.add_argument("--out", default="data/scanner/sightings.db",
                   help="sightings DB path (created; insert-only)")
    p.add_argument("--sample", type=int, default=500,
                   help="sample size (500 -> 200 ger/100 NULL/100 fre/"
                        "100 und+other)")
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                   help="comma list among " + ",".join(DEFAULT_SOURCES))
    p.add_argument("--isbn-file", default=None,
                   help="explicit ISBN list (one per line, '#' comments); "
                        "bypasses the stratified sampler entirely")
    p.add_argument("--max-seconds", type=float, default=7200.0,
                   help="wall-clock cap for the sweep")
    args = p.parse_args(argv)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in sources if s not in SOURCES]
    if bad:
        p.error(f"unknown source(s): {','.join(bad)}")
    conn = m4.connect(args.db)
    try:
        books = None
        if args.isbn_file:
            isbns = read_isbn_file(args.isbn_file)
            if not isbns:
                p.error(f"--isbn-file {args.isbn_file} lists no ISBNs")
            books = books_for_isbns(conn, isbns)
            if not books:
                p.error("--isbn-file: none of the listed ISBNs are in "
                        f"{args.db}")
        summary = run_pilot(conn, args.out, args.sample, sources,
                            max_seconds=args.max_seconds, books=books)
    finally:
        conn.close()
    return 0 if summary.get("books_done", 0) > 0 or not summary["sources"] \
        else 1


def main_english_set(argv=None) -> int:
    """scanner-english-set: the theory-directive English-first sweep set —
    wild-CR books with language eng/NULL/und/empty whose titles infer as
    English (or whose MARC code already says eng).  Pure read + CSV; the
    classifier stores NOTHING in the M4 DB."""
    import argparse
    p = argparse.ArgumentParser(
        prog="scanner-english-set",
        description="English-first sweep set: wild-CR books (language "
                    "eng/NULL/und/empty) whose titles infer English -> "
                    "CSV (isbn13, title, author, year, language_code, "
                    "lang_inferred).")
    p.add_argument("--db", default="lastcopy.db", help="M4/M5 SQLite DB path")
    p.add_argument("--out", default="english_wild.csv",
                   help="output CSV path")
    args = p.parse_args(argv)
    conn = m4.connect(args.db)
    try:
        rows = english_set_rows(conn)
    finally:
        conn.close()
    write_english_csv(rows, args.out)
    n_inferred = sum(1 for r in rows if r["lang_inferred"] == "eng")
    n_marc = sum(1 for r in rows if r["language_code"] == "eng")
    print(f"[english-set] {len(rows)} rows -> {args.out} "
          f"({n_inferred} title-inferred eng, {n_marc} MARC eng, "
          f"{n_inferred + n_marc} union)")
    return 0


def main_calibration(argv=None) -> int:
    """scanner-calibration: n random sighted books -> CSV hand-check
    sample (isbn13, title via m4 ATTACH, offerings_est, search links).
    The calibration step BEFORE tiers influence any scoring: Theory
    clicks the links and verifies our recorded counts against what the
    SERPs actually show."""
    import argparse
    import csv
    p = argparse.ArgumentParser(
        prog="scanner-calibration",
        description="Calibration hand-check sample: n random sighted "
                    "books with summed offerings + search links, so the "
                    "recorded counts can be verified by clicking through "
                    "(before circulation tiers influence any scoring).")
    p.add_argument("--db", default="data/scanner/sightings.db",
                   help="sightings DB path (read-only)")
    p.add_argument("--m4", default="lastcopy.db",
                   help="M4/M5 SQLite DB path, ATTACHed for titles")
    p.add_argument("--n", type=int, default=30,
                   help="sample size (random sighted books)")
    p.add_argument("--out", default="docs/scanner/calibration_sample.csv",
                   help="output CSV path")
    args = p.parse_args(argv)
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS m4db", (str(args.m4),))
    agg = market_aggregates(conn)
    isbns = [r["isbn13"] for r in conn.execute(
        "SELECT isbn13 FROM sightings WHERE isbn13 IS NOT NULL "
        "GROUP BY isbn13 ORDER BY RANDOM() LIMIT ?", (args.n,))]
    fieldnames = ("isbn13", "title", "offerings_est") \
        + tuple(f"link_{s}" for s in DEFAULT_SOURCES)
    rows: list[dict] = []
    for i13 in isbns:
        meta = conn.execute(
            "SELECT c.title, w.score FROM m4db.candidates c "
            "LEFT JOIN m4db.enrich_workset w ON w.isbn13 = c.isbn13 "
            "WHERE c.isbn13 = ?", (i13,)).fetchone()
        title = meta["title"] if meta is not None else None
        book = Book(i13, title, None, None, None, 0)
        row = {"isbn13": i13, "title": title or "",
               "offerings_est": agg.get(i13, {}).get("offerings", 0)}
        for src in DEFAULT_SOURCES:
            row[f"link_{src}"] = build_query(src, book)[0]
        rows.append(row)
    conn.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"[calibration] {len(rows)} books -> {args.out} "
          f"(offerings summed latest-per-source; click the links and "
          f"verify the counts)")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
