"""M5 — bulk enrichment + Book Red List classification (SPEC-M5-ENRICHMENT).

Deterministic, mostly-offline enrichment of the M4 candidate list's top-N
(``enrich_workset``) against BULK sources: the HathiTrust hathifiles TSV
(zero API), a saved Wikidata SPARQL snapshot (zero API), and batched
Internet Archive advancedsearch OR-queries (the ONLY network phase, run
explicitly via ``enrich-ia --execute``).  Status assignment
(``assign-status``) is pure Python rules + SQL.  Statuses are provisional
pending the later Google Books verification phase.

EW/EX statuses are reserved for future market/holdings phases (SPEC M5
"Out of scope"); only CR/EN/VU/NT/DD are assigned here.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import m4
from .isbn import normalize_isbn

WORKSET_LIMIT = 50_000          # SPEC M5: top-50k candidates
BATCH = 10_000                  # rows per executemany flush (RAM-bounding knob)
IA_BATCH = 30                   # keys per advancedsearch OR-query
IA_MAX_RETRIES = 3              # exponential backoff on 429/503
IA_MIN_INTERVAL = 1.0           # <=1 req/s rate discipline

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrich_workset (
  isbn13 TEXT PRIMARY KEY,
  work_key TEXT,
  edition_count INTEGER,
  score INTEGER,
  oclc TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS enrich_status (
  isbn13 TEXT PRIMARY KEY,
  ht_access TEXT,
  ia_identifier TEXT,
  wd_fulltext INTEGER,
  gb_status TEXT,
  sources_checked TEXT,
  checked_at TEXT,
  status TEXT,
  status_basis TEXT
);
CREATE TABLE IF NOT EXISTS ia_plan (
  element_idx INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  query TEXT NOT NULL,
  isbns TEXT NOT NULL,
  keys TEXT,
  done INTEGER NOT NULL DEFAULT 0,
  executed_at TEXT
);
"""

# hathifiles TSV column order (github.com/hathitrust/hathifiles README)
HT_COLS = ["htid", "access", "rights", "ht_bib_key", "description", "source",
           "source_bib_num", "oclc_num", "isbn", "issn", "lccn", "title",
           "imprint", "rights_reason_code", "rights_timestamp",
           "us_gov_doc_flag", "rights_date_used", "pub_place", "lang",
           "bib_fmt", "collection_code", "content_provider_code",
           "responsible_code", "digitization_agent_code",
           "access_profile_code", "author"]
HT_I_ACCESS = HT_COLS.index("access")
HT_I_OCLC = HT_COLS.index("oclc_num")
HT_I_ISBN = HT_COLS.index("isbn")

IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_FL = ["identifier", "isbn", "oclc"]

STATUS_ORDER = {"CR": 0, "EN": 1, "VU": 2, "NT": 3, "DD": 4}
PROVISIONAL_NOTE = "provisional pending Google Books verification"

_SPLIT_RE = re.compile(r"[,;\s]+")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------ workset
def build_workset(conn: sqlite3.Connection, limit: int = WORKSET_LIMIT) -> int:
    """Materialize the deterministic top-N of the candidates table
    (ORDER BY score DESC, isbn13 ASC LIMIT n) into enrich_workset and seed
    one enrich_status row per ISBN.  Idempotent (INSERT OR IGNORE)."""
    ensure_schema(conn)
    ts = now()
    conn.execute(
        """INSERT OR IGNORE INTO enrich_workset
             (isbn13, work_key, edition_count, score, oclc, created_at)
           SELECT c.isbn13, c.work_key, c.edition_count, c.score, NULL, ?
           FROM (SELECT isbn13, work_key, edition_count, score FROM candidates
                 ORDER BY score DESC, isbn13 ASC LIMIT ?) c""", (ts, limit))
    conn.execute(
        "INSERT OR IGNORE INTO enrich_status (isbn13) "
        "SELECT isbn13 FROM enrich_workset")
    conn.commit()
    return conn.execute("SELECT COUNT(*) c FROM enrich_workset").fetchone()["c"]


def _mark_checked(conn: sqlite3.Connection, source: str,
                  isbn13: str | None = None) -> None:
    """Append ``source`` to sources_checked (set semantics) for one row / all."""
    if isbn13 is not None:
        rows = conn.execute(
            "SELECT isbn13, sources_checked FROM enrich_status WHERE isbn13=?",
            (isbn13,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT isbn13, sources_checked FROM enrich_status").fetchall()
    updates = []
    for r in rows:
        have = {s for s in (r["sources_checked"] or "").split(",") if s}
        if source in have:
            continue
        have.add(source)
        updates.append((",".join(sorted(have)), now(), r["isbn13"]))
    if updates:
        conn.executemany(
            "UPDATE enrich_status SET sources_checked=?, checked_at=? "
            "WHERE isbn13=?", updates)
        conn.commit()


# ------------------------------------------------------------------ stage 0
def backfill_oclc(conn: sqlite3.Connection, dump: str | Path,
                  limit: int = WORKSET_LIMIT,
                  progress_every: int = m4.DEFAULT_PROGRESS_EVERY) -> dict:
    """Stage 0: stream the editions dump ONCE (same iteration path as
    ``m4._backfill_titles``); for records whose ISBN-13 set intersects the
    workset, store the first ``oclc_numbers`` value into
    ``enrich_workset.oclc``.  Same first-record-wins rule."""
    build_workset(conn, limit)
    workset = {r["isbn13"]
               for r in conn.execute("SELECT isbn13 FROM enrich_workset")}
    # bounded by |workset| (50k), never by the 100M-record dump
    updates: dict[str, str] = {}
    with m4.open_dump(path=dump) as fh:
        for key, obj in m4.iter_dump_records(fh, progress_every, "oclc"):
            if key == "_summary" or not key.startswith("/books/"):
                continue
            oclcs = obj.get("oclc_numbers") or []
            if not oclcs:
                continue
            oclc = str(m4._as_list(oclcs)[0])
            for raw in [*_m4_list(obj.get("isbn_13")),
                        *_m4_list(obj.get("isbn_10"))]:
                norm = normalize_isbn(str(raw))
                if norm and norm[0] in workset and norm[0] not in updates:
                    updates[norm[0]] = oclc
    if updates:
        conn.executemany("UPDATE enrich_workset SET oclc=? WHERE isbn13=?",
                         [(oclc, i13) for i13, oclc in updates.items()])
        conn.commit()
    return {"workset": len(workset), "oclc_backfilled": len(updates)}


def _m4_list(v) -> list:
    return m4._as_list(v)


# ------------------------------------------------------------------ stage 1
def enrich_ht(conn: sqlite3.Connection, hathifile: str | Path,
              progress_every: int = 1_000_000) -> dict:
    """Stage 1: stream-parse the hathifiles TSV (zero API), two-pass,
    RAM-bounded.  Pass 1 keeps only rows intersecting the workset (by ANY
    normalized ISBN, else by OCLC) into a staging table — never an 18M-row
    Python dict.  Pass 2 is one SQL upsert join per match key storing the
    BEST (most open) access per ISBN: allow > deny; among allows, first."""
    ensure_schema(conn)
    workset_oclc: dict[str, list[str]] = {}
    for r in conn.execute("SELECT isbn13, oclc FROM enrich_workset"):
        if r["oclc"]:
            for o in _SPLIT_RE.split(str(r["oclc"])):
                if o:
                    workset_oclc.setdefault(o, []).append(r["isbn13"])
    ws_isbns = {r["isbn13"]
                for r in conn.execute("SELECT isbn13 FROM enrich_workset")}

    conn.execute("DROP TABLE IF EXISTS ht_stage")
    conn.execute("CREATE TABLE ht_stage ("
                 "isbn13 TEXT, oclc TEXT, access TEXT)")
    rows: list[tuple] = []
    n_read = n_kept = 0
    # gzip-aware: real hathifiles ship as .txt.gz; fixtures may be plain
    opener = gzip.open if str(hathifile).endswith(".gz") else open
    with opener(hathifile, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n_read += 1
            if progress_every and n_read % progress_every == 0:
                print(f"[hathifile] {n_read:,} lines read ({n_kept:,} kept)",
                      file=sys.stderr, flush=True)
            f = line.rstrip("\n").split("\t")
            if len(f) <= HT_I_ISBN:
                continue
            access = f[HT_I_ACCESS].strip().lower()
            if access not in ("allow", "deny"):
                continue
            oclc_field = f[HT_I_OCLC].strip()
            matched_isbns = []
            oclc_hit = False
            for raw in _SPLIT_RE.split(f[HT_I_ISBN]):
                norm = normalize_isbn(raw)
                if norm and norm[0] in ws_isbns:
                    matched_isbns.append(norm[0])
            if not matched_isbns and oclc_field:
                for o in _SPLIT_RE.split(oclc_field):
                    if o in workset_oclc:
                        oclc_hit = True
                        break
            if not matched_isbns and not oclc_hit:
                continue
            oclc_val = next((o for o in _SPLIT_RE.split(oclc_field) if o), None)
            for i13 in matched_isbns:
                rows.append((i13, oclc_val, access))
                n_kept += 1
            if oclc_hit and not matched_isbns:
                rows.append((None, oclc_val, access))
                n_kept += 1
            if len(rows) >= BATCH:
                _flush_ht(conn, rows)
    _flush_ht(conn, rows)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ht_stage_isbn ON ht_stage(isbn13)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ht_stage_oclc ON ht_stage(oclc)")

    # pass 2 — single SQL upsert joins (idempotent)
    conn.execute(
        """UPDATE enrich_status SET ht_access = (
             SELECT CASE WHEN COUNT(*) = 0 THEN NULL
                         WHEN SUM(CASE WHEN s.access='allow' THEN 1 ELSE 0 END) > 0
                         THEN 'allow' ELSE 'deny' END
             FROM ht_stage s WHERE s.isbn13 = enrich_status.isbn13)""")
    conn.execute(
        """UPDATE enrich_status SET ht_access = (
             SELECT CASE WHEN COUNT(*) = 0 THEN NULL
                         WHEN SUM(CASE WHEN s.access='allow' THEN 1 ELSE 0 END) > 0
                         THEN 'allow' ELSE 'deny' END
             FROM ht_stage s JOIN enrich_workset w ON w.isbn13 = enrich_status.isbn13
             WHERE s.oclc = w.oclc)
           WHERE ht_access IS NULL AND EXISTS (
             SELECT 1 FROM enrich_workset w WHERE w.isbn13 = enrich_status.isbn13
               AND w.oclc IS NOT NULL)""")
    conn.execute("DROP TABLE ht_stage")
    conn.commit()
    _mark_checked(conn, "ht")
    counts = {"allow": _count(conn, "ht_access='allow'"),
              "deny": _count(conn, "ht_access='deny'")}
    return {"lines_read": n_read, "staged_rows": n_kept, "ht": counts}


def _flush_ht(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if rows:
        conn.executemany("INSERT INTO ht_stage (isbn13, oclc, access) "
                         "VALUES (?,?,?)", rows)
        rows.clear()
        conn.commit()


def _count(conn: sqlite3.Connection, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) c FROM enrich_status WHERE {where}"
                        ).fetchone()["c"]


# ------------------------------------------------------------------ stage 2
def enrich_wikidata(conn: sqlite3.Connection, results_path: str | Path) -> dict:
    """Stage 2: parse the saved SPARQL JSON snapshot (fetched once by the
    pipeline runner; this function stays offline/deterministic).
    ``wd_fulltext`` = 1 when the ISBN has a full-text work link."""
    ensure_schema(conn)
    workset = {r["isbn13"]
               for r in conn.execute("SELECT isbn13 FROM enrich_workset")}
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    bindings = data.get("results", {}).get("bindings", [])
    matched: set[str] = set()
    for b in bindings:
        if not isinstance(b, dict):
            continue
        isbn_var = b.get("isbn") or b.get("isbn13")
        if not isbn_var:
            continue
        link = b.get("url") or b.get("fulltext") or b.get("work")
        if link is not None and not str(link.get("value", "")).strip():
            continue  # explicit empty link -> no full text
        norm = normalize_isbn(str(isbn_var.get("value", "")))
        if norm and norm[0] in workset:
            matched.add(norm[0])
    conn.execute("UPDATE enrich_status SET wd_fulltext=0")
    if matched:
        conn.executemany("UPDATE enrich_status SET wd_fulltext=1 WHERE isbn13=?",
                         [(i,) for i in sorted(matched)])
    conn.commit()
    _mark_checked(conn, "wd")
    return {"bindings": len(bindings), "wd_fulltext": len(matched)}


# ------------------------------------------------------------------ stage 3
def build_ia_plan(conn: sqlite3.Connection, batch: int = IA_BATCH) -> dict:
    """Stage 3 plan mode: workset ISBNs not already matched (ht allow OR
    wd_fulltext=1 OR ia hit), grouped ``batch`` per advancedsearch OR-query,
    deterministic isbn13 ASC order; then OCLC OR-queries for still-unresolved
    rows that have OCLC.  Plan is fully regenerable: rerun rebuilds the same
    element_idx sequence."""
    ensure_schema(conn)
    conn.execute("DELETE FROM ia_plan")
    conn.execute(
        """CREATE TEMP TABLE IF NOT EXISTS plan_seed (
             isbn13 TEXT PRIMARY KEY, oclc TEXT)""")
    conn.execute("DELETE FROM plan_seed")
    conn.execute(
        """INSERT INTO plan_seed
           SELECT w.isbn13, w.oclc FROM enrich_workset w
           JOIN enrich_status s ON s.isbn13 = w.isbn13
           WHERE COALESCE(s.ht_access,'') <> 'allow'
             AND COALESCE(s.wd_fulltext,0) <> 1
             AND s.ia_identifier IS NULL
           ORDER BY w.isbn13 ASC""")
    elems: list[tuple[int, str, str, list[str], list[str | None]]] = []
    idx = 0
    isbns = [r["isbn13"] for r in
             conn.execute("SELECT isbn13 FROM plan_seed ORDER BY isbn13 ASC")]
    for i in range(0, len(isbns), batch):
        chunk = isbns[i:i + batch]
        q = " OR ".join(f"isbn:{v}" for v in chunk)
        elems.append((idx, "isbn", q, chunk, [None] * len(chunk)))
        idx += 1
    rows = conn.execute(
        """SELECT isbn13, oclc FROM plan_seed
           WHERE oclc IS NOT NULL AND oclc <> '' ORDER BY isbn13 ASC""").fetchall()
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        q = " OR ".join(f"oclc:{r['oclc']}" for r in chunk)
        elems.append((idx, "oclc", q, [r["isbn13"] for r in chunk],
                      [r["oclc"] for r in chunk]))
        idx += 1
    conn.executemany(
        "INSERT INTO ia_plan (element_idx, kind, query, isbns, keys) "
        "VALUES (?,?,?,?,?)",
        [(e[0], e[1], e[2], json.dumps(e[3]), json.dumps(e[4])) for e in elems])
    conn.commit()
    return {"elements": len(elems),
            "isbn_isbns": len(isbns),
            "oclc_isbns": len(rows)}


def _http_get(url: str, params: dict):
    """Default (real) transport: httpx sync client.  Executor accepts any
    requests-style injectable ``get`` for tests."""
    import httpx
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        return client.get(url, params=params)


def execute_ia_element(conn: sqlite3.Connection, element_idx: int, *,
                       get=_http_get, sleep=time.sleep,
                       min_interval: float = IA_MIN_INTERVAL,
                       max_retries: int = IA_MAX_RETRIES) -> dict:
    """Execute one ia_plan element with rate discipline (<=1 req/s,
    exponential backoff on 429/503, max ``max_retries``).  Deterministic +
    resumable: each result is upserted; reruns skip done elements and skip
    ISBNs already resolved by an earlier phase."""
    row = conn.execute("SELECT * FROM ia_plan WHERE element_idx=?",
                       (element_idx,)).fetchone()
    if row is None:
        raise ValueError(f"no ia_plan element {element_idx}")
    if row["done"]:
        return {"element": element_idx, "skipped_done": True, "hits": 0}
    isbns: list[str] = json.loads(row["isbns"])
    keys: list = json.loads(row["keys"]) if row["keys"] else [None] * len(isbns)
    kind: str = row["kind"]

    live = [i13 for i13 in isbns if not _resolved(conn, i13)]
    hits = 0
    if live:
        params = {"q": row["query"], "rows": len(live), "output": "json",
                  **{f"fl[{i}]": f for i, f in enumerate(IA_FL)}}
        data = _request_json(get, params, sleep, max_retries, min_interval)
        if data is not None:
            hits = _record_ia_hits(conn, kind, live, keys,
                                   data.get("response", {}).get("docs", []))
    conn.execute("UPDATE ia_plan SET done=1, executed_at=? WHERE element_idx=?",
                 (now(), element_idx))
    conn.commit()
    _mark_checked(conn, "ia")
    return {"element": element_idx, "skipped_done": False, "hits": hits}


def _resolved(conn: sqlite3.Connection, isbn13: str) -> bool:
    r = conn.execute(
        "SELECT ht_access, wd_fulltext, ia_identifier FROM enrich_status "
        "WHERE isbn13=?", (isbn13,)).fetchone()
    return bool(r and (r["ht_access"] == "allow" or r["wd_fulltext"] == 1
                       or r["ia_identifier"]))


def _request_json(get, params: dict, sleep, max_retries: int,
                  min_interval: float = IA_MIN_INTERVAL):
    attempt = 0
    while True:
        sleep(min_interval)          # rate discipline: <=1 req/s
        resp = get(IA_SEARCH_URL, params)
        if resp.status_code in (429, 503) and attempt < max_retries - 1:
            backoff = 2.0 ** attempt  # exponential backoff on 429/503
            sleep(backoff)
            attempt += 1
            continue
        if getattr(resp, "ok", 200 <= resp.status_code < 300):
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError):
                return None
            return None if "error" in data else data
        return None


def _record_ia_hits(conn: sqlite3.Connection, kind: str,
                    live_isbns: list[str], keys: list, docs: list) -> int:
    if kind == "isbn":
        targets = {i13: i13 for i13 in live_isbns}
    else:
        targets = {str(k): i13 for k, i13 in zip(keys, live_isbns) if k}
    hits = 0
    for d in docs:
        if not isinstance(d, dict):
            continue
        ident = d.get("identifier")
        if not ident:
            continue
        if kind == "isbn":
            doc_vals = set()
            for raw in _as_str_list(d.get("isbn")):
                norm = normalize_isbn(raw)
                if norm:
                    doc_vals.add(norm[0])
            matched = sorted(doc_vals & set(live_isbns))
        else:
            doc_oclcs = {str(v) for v in _as_str_list(d.get("oclc"))}
            matched = sorted(i13 for key, i13 in targets.items() if key in doc_oclcs)
        for i13 in matched:
            conn.execute(
                "UPDATE enrich_status SET ia_identifier=? WHERE isbn13=? "
                "AND ia_identifier IS NULL", (str(ident), i13))
            hits += 1
    conn.commit()
    return hits


def _as_str_list(v) -> list[str]:
    if v is None:
        return []
    vals = v if isinstance(v, list) else [v]
    out: list[str] = []
    for x in vals:
        for part in _SPLIT_RE.split(str(x)):
            if part:
                out.append(part)
    return out


# ------------------------------------------------------------------ stage 4
def assign_status(conn: sqlite3.Connection) -> dict:
    """Stage 4: Book Red List rules (pure Python rules + SQL; no network).

    Precedence (SPEC-M5-ENRICHMENT "Stage 4"):
      1. digital_full (ht allow OR wd_fulltext=1 OR ia hit) -> NT
      2. no signals resolvable (absent from HT/IA/WD checks AND no OCLC AND
         not IA-queryable) -> DD
      3. else by edition_count: 1 -> CR, 2-3 -> EN, >=4 -> VU
    digital_partial is conservatively HT deny only for now (does not affect
    status).  EW/EX reserved for future market/holdings phases."""
    ensure_schema(conn)
    from collections import Counter
    counts: Counter = Counter()
    rows = conn.execute(
        """SELECT s.isbn13, s.ht_access, s.ia_identifier, s.wd_fulltext,
                  s.sources_checked, w.edition_count, w.oclc
           FROM enrich_status s JOIN enrich_workset w ON w.isbn13 = s.isbn13"""
    ).fetchall()
    updates = []
    for r in rows:
        status, basis = _rule(r)
        updates.append((status, basis, r["isbn13"]))
        counts[status] += 1
    conn.executemany(
        "UPDATE enrich_status SET status=?, status_basis=? WHERE isbn13=?",
        updates)
    conn.commit()
    return {"assigned": len(updates), "counts": dict(counts)}


def _rule(r) -> tuple[str, str]:
    full_srcs = []
    if r["ht_access"] == "allow":
        full_srcs.append("ht allow")
    if r["wd_fulltext"] == 1:
        full_srcs.append("wd fulltext")
    if r["ia_identifier"]:
        full_srcs.append("ia hit")
    if full_srcs:
        return "NT", f"full digital exists ({'+'.join(full_srcs)}); artifact may still be scarce"
    edition_count = r["edition_count"] or 1
    checked = {s for s in (r["sources_checked"] or "").split(",") if s}
    ia_checked = "ia" in checked
    ia_hit_absent = r["ia_identifier"] is None
    if (r["ht_access"] is None and r["wd_fulltext"] != 1 and ia_hit_absent
            and not r["oclc"] and ia_checked):
        return "DD", ("no signals resolvable: absent from HT/WD/IA checks; "
                      "no OCLC; not IA-queryable")
    if edition_count == 1:
        return "CR", "edition_count=1; no digital in HT/IA/WD"
    if edition_count <= 3:
        return "EN", f"edition_count={edition_count}; no digital in HT/IA/WD"
    return "VU", f"edition_count={edition_count}; no digital in HT/IA/WD"


# ------------------------------------------------------------------ export
WORKSET_HEADER = ["isbn13", "title", "author", "year", "language",
                  "edition_count", "score", "status", "status_basis"]


def export_workset(conn: sqlite3.Connection, csv_path: str | Path,
                   md_path: str | Path | None = None) -> dict:
    """Export the workset WITH status columns, ordered by status severity
    (CR, EN, VU, NT, DD) then score DESC, isbn13 ASC.  Appends the
    provisional note to exported lists."""
    ensure_schema(conn)
    order = "CASE s.status WHEN 'CR' THEN 0 WHEN 'EN' THEN 1 WHEN 'VU' THEN 2 " \
            "WHEN 'NT' THEN 3 WHEN 'DD' THEN 4 ELSE 5 END"
    sql = f"""SELECT s.isbn13, c.title, c.year, c.language, w.edition_count,
                     w.score, s.status, s.status_basis, wr.author_keys
              FROM enrich_workset w
              JOIN enrich_status s ON s.isbn13 = w.isbn13
              LEFT JOIN candidates c ON c.isbn13 = w.isbn13
              LEFT JOIN works_ref wr ON wr.work_key = w.work_key
              ORDER BY {order}, w.score DESC, w.isbn13 ASC"""
    rows = conn.execute(sql).fetchall()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(WORKSET_HEADER)
        for r in rows:
            wtr.writerow([r["isbn13"], r["title"],
                          m4.resolve_authors(conn, r["author_keys"]),
                          r["year"], r["language"], r["edition_count"],
                          r["score"], r["status"], r["status_basis"]])
        wtr.writerow([f"# {PROVISIONAL_NOTE}"])
    if md_path:
        lines = ["# lastcopy Book Red List — workset (provisional)", "",
                 f"Top {len(rows)} enriched workset rows. "
                 f"{PROVISIONAL_NOTE.capitalize()}.", "",
                 "| # | isbn13 | title | author | year | lang | eds | score "
                 "| status | status_basis |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            title = str(r["title"] or "").replace("|", "\\|")
            author = m4.resolve_authors(conn, r["author_keys"]).replace("|", "\\|")
            lines.append(f"| {i} | {r['isbn13']} | {title} | {author} "
                         f"| {r['year']} | {r['language'] or ''} "
                         f"| {r['edition_count']} | {r['score']} "
                         f"| {r['status']} | {r['status_basis']} |")
        lines += ["", f"_{PROVISIONAL_NOTE}._", ""]
        Path(md_path).write_text("\n".join(lines), encoding="utf-8")
    return {"exported": len(rows), "csv": str(csv_path), "md": str(md_path or "")}
