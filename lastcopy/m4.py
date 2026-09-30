"""M4 — THE LIST: offline candidate generator from Open Library CC0 dumps.

Zero live API calls (the ``curl`` in ingest-editions is a dumb file transport).
RAM-bounded (SPEC: <=1.5G peak on a 3G host): every dump is streamed
line-by-line with batched SQLite writes; nothing is materialized in memory.
Per-work edition counting is done **in SQLite** (a final GROUP-BY backfill),
never in a Python dict — so a mid-stream restart of the editions dump can
never double-count. The 9.2G editions dump is NEVER written to disk: it is
consumed as ``curl | gunzip | parse`` straight into ``editions_ref``.

Restart-on-failure (documented, SPEC M4): if the gzip stream dies mid-way
(truncation, dropped connection, curl nonzero exit), the whole stream is
restarted from zero, up to ``retries`` times. ``editions_ref`` upserts are
idempotent (PK isbn13) and counting is recomputed after success, so a
restart yields exactly the same state as an uninterrupted run.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import io
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from .isbn import normalize_isbn

BATCH = 10_000                    # rows per executemany flush (RAM-bounding knob)
DEFAULT_PROGRESS_EVERY = 1_000_000  # SPEC M4: progress log every 1M records
DEFAULT_RETRIES = 3
DEFAULT_EDITIONS_URL = (
    "https://openlibrary.org/data/ol_dump_editions_latest.txt.gz"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS works_ref (
  work_key TEXT PRIMARY KEY,
  edition_count INTEGER NOT NULL DEFAULT 0,
  author_keys TEXT
);
CREATE TABLE IF NOT EXISTS authors_ref (
  author_key TEXT PRIMARY KEY,
  name TEXT
);
CREATE TABLE IF NOT EXISTS editions_ref (
  isbn13 TEXT PRIMARY KEY,
  edition_key TEXT,
  work_key TEXT,
  year INTEGER,
  language TEXT,
  ia TEXT
);
CREATE INDEX IF NOT EXISTS idx_editions_ref_work ON editions_ref(work_key);
CREATE TABLE IF NOT EXISTS candidates (
  isbn13 TEXT PRIMARY KEY,
  work_key TEXT,
  title TEXT,
  year INTEGER,
  language TEXT,
  edition_count INTEGER,
  score INTEGER,
  rationale TEXT
);
"""

# Scoring weights (deterministic extinction prior, SPEC M4 item 3).
W_EDITIONS = {1: 3, 2: 2, 3: 1}   # <=max filter already bounds the range; 4+ -> 1
W_LANG_ENG, W_LANG_UNKNOWN, W_LANG_OTHER = 0, 1, 2
W_AGE_PRE1927, W_AGE_PRE1970, W_AGE_MODERN = 3, 2, 0


class StreamFailed(Exception):
    """The editions dump stream died mid-way (restart from zero)."""


def connect(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ------------------------------------------------------------------ dump parsing
def iter_dump_records(text_fh, progress_every: int = DEFAULT_PROGRESS_EVERY,
                      label: str = "dump"):
    """Yield (key, obj) per line of an OL dump (type\tkey\trevision\tlast_mod\tJSON).

    Real dump lines can carry escaped tabs/quotes inside the JSON blob, so the
    line is split on the first 4 tabs only. Malformed lines are counted, not
    fatal. Progress log every ``progress_every`` records (1M in production).
    """
    n = bad = kept = 0
    for line in text_fh:
        n += 1
        parts = line.rstrip("\n").split("\t", 4)
        if len(parts) != 5:
            bad += 1
            continue
        try:
            obj = json.loads(parts[4])
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(obj, dict):
            bad += 1
            continue
        kept += 1
        if progress_every and n % progress_every == 0:
            print(f"[{label}] {n:,} records read ({kept:,} parsed, {bad:,} bad)",
                  file=sys.stderr, flush=True)
        yield parts[1], obj
    if progress_every:
        print(f"[{label}] done: {n:,} records read ({kept:,} parsed, {bad:,} bad)",
              file=sys.stderr, flush=True)
    yield "_summary", {"read": n, "parsed": kept, "bad": bad}


@contextlib.contextmanager
def open_dump(path: str | Path | None = None, url: str | None = None):
    """Open a gzipped OL dump as a text stream, never writing it to disk.

    ``path`` -> local gzip file (fixture / Apprentice-downloaded works+authors
    dumps). ``url`` -> ``curl -sSL <url>`` piped straight into gzip; the
    decompressed bytes exist only in the pipe + SQLite.
    """
    if path is not None and url is not None:
        raise ValueError("pass either --file or --stream-url, not both")
    if path is not None:
        with gzip.open(path, "rb") as raw:
            yield io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        return
    if url is None:
        raise ValueError("one of --file / --stream-url is required")
    proc = subprocess.Popen(
        ["curl", "-sSL", url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    gz = gzip.GzipFile(fileobj=proc.stdout)
    try:
        yield io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
    finally:
        proc.stdout.close()
        rc = proc.wait()
        if rc != 0:
            raise StreamFailed(f"curl exited {rc} (url: {url})")


# ------------------------------------------------------------------ field extraction
_YEAR_RE = re.compile(r"\b(\d{4})\b")


def parse_year(publish_date) -> int | None:
    """Lenient publish_date -> year: first plausible 4-digit number."""
    if not publish_date:
        return None
    for tok in _YEAR_RE.findall(str(publish_date)):
        y = int(tok)
        if 1440 <= y <= 2077:  # sanity window; OL has typos like 2202
            return y
    return None


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def extract_edition(key: str, obj: dict) -> list[dict]:
    """Reduce an OL edition record to ISBN-keyed rows ([] if no valid ISBN).

    Multi-ISBN records yield one row per distinct valid ISBN-13 (isbn_13 first,
    then isbn_10 converted). ISBN-10-only records are kept via conversion.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in [*_as_list(obj.get("isbn_13")), *_as_list(obj.get("isbn_10"))]:
        norm = normalize_isbn(str(raw))
        if norm and norm[0] not in seen:
            seen.add(norm[0])
            ordered.append(norm[0])
    if not ordered:
        return []
    year = parse_year(obj.get("publish_date"))
    langs = [str(l).rsplit("/", 1)[-1] for l in _as_list(obj.get("languages"))]
    works = [w.get("key") for w in obj.get("works") or [] if isinstance(w, dict)]
    ia = ",".join(str(x) for x in _as_list(obj.get("ia")))
    base = dict(edition_key=key, work_key=works[0] if works else None,
                year=year, language=langs[0] if langs else None, ia=ia or None)
    return [dict(base, isbn13=i13) for i13 in ordered]


# ------------------------------------------------------------------ ingesters
def ingest_works(conn: sqlite3.Connection, works_file: str | Path,
                 authors_file: str | Path,
                 progress_every: int = DEFAULT_PROGRESS_EVERY) -> dict:
    """Stored works + authors dumps -> works_ref / authors_ref (batched upserts)."""
    stats = {"works": 0, "authors": 0}
    batch: list[tuple] = []
    for key, obj in iter_dump_records(gzip.open(works_file, "rt", encoding="utf-8",
                                                errors="replace"),
                                      progress_every, "works"):
        if key == "_summary":
            continue
        if key.startswith("/works/"):
            akeys = json.dumps([a.get("key") for a in obj.get("authors") or []
                                if isinstance(a, dict) and a.get("key")])
            batch.append((key, akeys))
            stats["works"] += 1
        if len(batch) >= BATCH:
            conn.executemany(
                "INSERT OR REPLACE INTO works_ref (work_key, author_keys) VALUES (?,?)",
                batch)
            batch.clear()
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO works_ref (work_key, author_keys) VALUES (?,?)",
            batch)
    batch = []
    for key, obj in iter_dump_records(gzip.open(authors_file, "rt", encoding="utf-8",
                                                errors="replace"),
                                      progress_every, "authors"):
        if key == "_summary":
            continue
        if key.startswith("/authors/") and obj.get("name"):
            batch.append((key, str(obj["name"])))
            stats["authors"] += 1
        if len(batch) >= BATCH:
            conn.executemany(
                "INSERT OR REPLACE INTO authors_ref (author_key, name) VALUES (?,?)",
                batch)
            batch.clear()
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO authors_ref (author_key, name) VALUES (?,?)",
            batch)
    conn.commit()
    _backfill_edition_counts(conn)
    return stats


def _backfill_edition_counts(conn: sqlite3.Connection) -> None:
    """Per-work edition counting, in SQLite (never a RAM dict). Idempotent.
    Counts DISTINCT edition_key so a multi-ISBN edition counts as one edition."""
    conn.execute(
        """UPDATE works_ref SET edition_count = COALESCE(
             (SELECT COUNT(DISTINCT edition_key) FROM editions_ref
              WHERE editions_ref.work_key = works_ref.work_key), 0)""")
    conn.commit()


def ingest_editions(conn: sqlite3.Connection, *, file: str | Path | None = None,
                    url: str | None = None,
                    progress_every: int = DEFAULT_PROGRESS_EVERY,
                    retries: int = DEFAULT_RETRIES,
                    _opener=open_dump) -> dict:
    """Stream the editions dump (file or curl|gunzip) -> editions_ref.

    Only ISBN-keyed records are kept (SPEC M4 item 2). Mid-stream failure
    restarts the stream from zero (up to ``retries``); safe because upserts
    are idempotent and counting is a post-hoc SQL backfill.
    """
    kept = 0
    attempt = 0
    while True:
        try:
            with _opener(path=file, url=url) as fh:
                batch: list[tuple] = []
                for key, obj in iter_dump_records(fh, progress_every, "editions"):
                    if key == "_summary":
                        continue
                    if not key.startswith("/books/"):
                        continue
                    for row in extract_edition(key, obj):
                        batch.append((row["isbn13"], row["edition_key"],
                                      row["work_key"], row["year"],
                                      row["language"], row["ia"]))
                        kept += 1
                    if len(batch) >= BATCH:
                        conn.executemany(
                            """INSERT OR REPLACE INTO editions_ref
                               (isbn13, edition_key, work_key, year, language, ia)
                               VALUES (?,?,?,?,?,?)""", batch)
                        conn.commit()
                        batch.clear()
                if batch:
                    conn.executemany(
                        """INSERT OR REPLACE INTO editions_ref
                           (isbn13, edition_key, work_key, year, language, ia)
                           VALUES (?,?,?,?,?,?)""", batch)
                    conn.commit()
            break
        except (StreamFailed, EOFError, gzip.BadGzipFile, OSError) as exc:
            if attempt >= retries:
                raise
            attempt += 1
            print(f"[editions] stream failed after {kept:,} kept records "
                  f"({exc!r}); restarting from zero "
                  f"(attempt {attempt}/{retries}; documented M4 restart behavior)",
                  file=sys.stderr, flush=True)
    _backfill_edition_counts(conn)
    return {"isbn_keyed_rows": kept, "restarts": attempt}


# ------------------------------------------------------------------ candidates
def score_candidate(edition_count: int | None, language: str | None,
                    year: int | None) -> tuple[int, str]:
    """Deterministic extinction-prior score (SPEC M4 item 3).

    Pure function of (edition_count, language, year) -> identical rows always
    score identically. Rationale is machine-readable and cites every weight.
    """
    ec = edition_count if edition_count and edition_count > 0 else 1
    ed_w = W_EDITIONS.get(ec, 1)
    if language == "eng":
        lang_b, lang_tag = W_LANG_ENG, "eng"
    elif language:
        lang_b, lang_tag = W_LANG_OTHER, language
    else:
        lang_b, lang_tag = W_LANG_UNKNOWN, "unknown"
    if year is not None and year < 1927:
        age_b, era = W_AGE_PRE1927, "pre-1927"
    elif year is not None and year < 1970:
        age_b, era = W_AGE_PRE1970, "1927-1969"
    elif year is not None:
        age_b, era = W_AGE_MODERN, "1970+"
    else:
        age_b, era = 0, "unknown"
    rationale = (f"editions={ec}(+{ed_w}); lang={lang_tag}(+{lang_b}); "
                 f"era={era}(+{age_b})")
    return ed_w + lang_b + age_b, rationale


def gen_candidates(conn: sqlite3.Connection, *, max_editions: int = 1,
                   lang: str | None = None, from_year: int | None = None,
                   to_year: int | None = None) -> dict:
    """editions_ref x works_ref join: IA-flag empty AND edition_count <= max."""
    sql = """SELECT e.isbn13, e.work_key, e.year, e.language, w.edition_count
             FROM editions_ref e JOIN works_ref w ON e.work_key = w.work_key
             WHERE (e.ia IS NULL OR e.ia = '') AND w.edition_count <= ?"""
    params: list = [max_editions]
    if lang:
        sql += " AND e.language = ?"
        params.append(lang)
    if from_year is not None:
        sql += " AND e.year >= ?"
        params.append(from_year)
    if to_year is not None:
        sql += " AND e.year <= ?"
        params.append(to_year)

    conn.execute("DELETE FROM candidates")
    batch: list[tuple] = []
    n = 0
    for row in conn.execute(sql, params):
        score, rationale = score_candidate(row["edition_count"], row["language"],
                                           row["year"])
        batch.append((row["isbn13"], row["work_key"], None, row["year"],
                      row["language"], row["edition_count"], score, rationale))
        n += 1
        if len(batch) >= BATCH:
            conn.executemany(
                """INSERT OR REPLACE INTO candidates (isbn13, work_key, title, year,
                     language, edition_count, score, rationale)
                   VALUES (?,?,?,?,?,?,?,?)""", batch)
            batch.clear()
    if batch:
        conn.executemany(
            """INSERT OR REPLACE INTO candidates (isbn13, work_key, title, year,
                 language, edition_count, score, rationale)
               VALUES (?,?,?,?,?,?,?,?)""", batch)
    conn.commit()
    return {"candidates": n, "filters": {"max_editions": max_editions, "lang": lang,
                                         "from_year": from_year, "to_year": to_year}}


def resolve_authors(conn: sqlite3.Connection, author_keys_json: str | None) -> str:
    """works_ref.author_keys -> names via authors_ref ('; '-joined, keys as fallback)."""
    if not author_keys_json:
        return ""
    try:
        keys = json.loads(author_keys_json)
    except (json.JSONDecodeError, TypeError):
        keys = []
    names = []
    for k in keys or []:
        row = conn.execute("SELECT name FROM authors_ref WHERE author_key=?",
                           (k,)).fetchone()
        names.append(row["name"] if row and row["name"] else k.rsplit("/", 1)[-1])
    return "; ".join(names)


# ------------------------------------------------------------------ export
LIST_HEADER = ["isbn13", "title", "author", "year", "language",
               "edition_count", "score", "rationale"]


def _backfill_titles(conn: sqlite3.Connection, rows, editions_dump) -> int:
    """Stream the editions dump once; fill winners' titles from record JSON.

    Downstream only the top winners' titles are ever used, so titles are not
    stored in ``editions_ref`` (slim schema); this single pass re-derives them.
    For any record whose generated ISBN-13 set intersects the winners' ISBNs,
    the title precedence is exactly what the fat path stored:
    ``title or full_title or subtitle``. First matching record wins per ISBN.
    """
    winners = {r["isbn13"] for r in rows}
    titles: dict[str, str] = {}
    with open_dump(path=editions_dump) as fh:
        for key, obj in iter_dump_records(fh, 0, "titles"):
            if key == "_summary" or not key.startswith("/books/"):
                continue
            title = obj.get("title") or obj.get("full_title") or obj.get("subtitle")
            if not title:
                continue
            for raw in [*_as_list(obj.get("isbn_13")), *_as_list(obj.get("isbn_10"))]:
                norm = normalize_isbn(str(raw))
                if norm and norm[0] in winners and norm[0] not in titles:
                    titles[norm[0]] = str(title)
    if titles:
        conn.executemany("UPDATE candidates SET title=? WHERE isbn13=?",
                         [(t, i13) for i13, t in titles.items()])
        conn.commit()
    return len(titles)


def export_list(conn: sqlite3.Connection, top: int, csv_path: str | Path,
                md_path: str | Path | None = None,
                editions_dump: str | Path | None = None) -> dict:
    """THE LIST (CC0): top-N candidates; CSV feeds registry `ingest --csv` unchanged.

    ``editions_dump`` (optional): one streaming pass over the editions dump to
    backfill winners' titles (slim editions_ref stores no titles). None ->
    titles stay NULL and CSV/MD emit them empty.
    """
    sql = """SELECT c.isbn13, c.title, c.year, c.language, c.edition_count,
                    c.score, c.rationale, w.author_keys
             FROM candidates c JOIN works_ref w ON c.work_key = w.work_key
             ORDER BY c.score DESC, c.isbn13 ASC LIMIT ?"""
    rows = conn.execute(sql, (top,)).fetchall()
    filled = 0
    if editions_dump is not None and rows:
        filled = _backfill_titles(conn, rows, editions_dump)
        rows = conn.execute(sql, (top,)).fetchall()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(LIST_HEADER)
        for r in rows:
            w.writerow([r["isbn13"], r["title"], resolve_authors(conn, r["author_keys"]),
                        r["year"], r["language"], r["edition_count"], r["score"],
                        r["rationale"]])
    if md_path:
        lines = ["# lastcopy candidate list (CC0)", "",
                 f"Top {len(rows)} of the ranked extinction-prior candidate list "
                 "(offline OL dumps; IA-flag empty, edition_count at or below cap).", "",
                 "| # | isbn13 | title | author | year | lang | eds | score | rationale |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            title = str(r["title"] or "").replace("|", "\\|")
            author = resolve_authors(conn, r["author_keys"]).replace("|", "\\|")
            lines.append(f"| {i} | {r['isbn13']} | {title} | {author} | {r['year']} "
                         f"| {r['language'] or ''} | {r['edition_count']} "
                         f"| {r['score']} | {r['rationale']} |")
        lines.append("")
        Path(md_path).write_text("\n".join(lines), encoding="utf-8")
    return {"exported": len(rows), "csv": str(csv_path), "md": str(md_path or ""),
            "titles_backfilled": filled}
