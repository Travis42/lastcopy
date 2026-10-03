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
  ia_source TEXT,
  wd_fulltext INTEGER,
  gb_status TEXT,
  gb_identifier TEXT,
  sources_checked TEXT,
  checked_at TEXT,
  status TEXT,
  status_basis TEXT,
  custody TEXT,
  custody_physical TEXT,
  holdings TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
  isbn13 TEXT NOT NULL,
  institution TEXT NOT NULL,
  record_id TEXT,
  PRIMARY KEY (isbn13, institution)
);
CREATE TABLE IF NOT EXISTS ocaid_stage (
  work_key TEXT NOT NULL,
  ocaid TEXT NOT NULL,
  edition_key TEXT NOT NULL,
  PRIMARY KEY (work_key, edition_key)
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
# Container/pallet items (bwb_daily_pallets_*, BWB-*) carry hundreds of ISBNs
# each but are NOT scans — bulk-donation inventory (2026-09-30 real-data bug).
# Query-side: AND mediatype:texts. Result-side belt-and-braces: reject ids.
IA_CONTAINER_RE = re.compile(r"^bwb", re.IGNORECASE)   # any BWB-prefixed id is a pallet/container (incl. BWB20151104Xisbn, no separator)
IA_QUERY_SUFFIX = " AND mediatype:texts"

STATUS_ORDER = {"CR": 0, "EN": 1, "VU": 2, "NT": 3, "DD": 4}
PROVISIONAL_NOTE = "provisional pending Google Books verification"

_SPLIT_RE = re.compile(r"[,;\s]+")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # M5.5 migration: ia_source / gb_identifier on pre-existing DBs
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(enrich_status)")}
    for col in ("ia_source", "gb_identifier", "custody", "custody_physical",
                "holdings", "pg_id", "gallica_ark"):
        if col not in cols:
            conn.execute(f"ALTER TABLE enrich_status ADD COLUMN {col} TEXT")
    # direct IA-search hits predate the column -> 'search' (ocaid rows keep 'ocaid')
    conn.execute("UPDATE enrich_status SET ia_source='search' "
                 "WHERE ia_identifier IS NOT NULL AND ia_source IS NULL")
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
              AND s.pg_id IS NULL
              AND s.gallica_ark IS NULL
           ORDER BY w.isbn13 ASC""")
    elems: list[tuple[int, str, str, list[str], list[str | None]]] = []
    idx = 0
    isbns = [r["isbn13"] for r in
             conn.execute("SELECT isbn13 FROM plan_seed ORDER BY isbn13 ASC")]
    for i in range(0, len(isbns), batch):
        chunk = isbns[i:i + batch]
        q = "(" + " OR ".join(f"isbn:{v}" for v in chunk) + IA_QUERY_SUFFIX + ")"
        elems.append((idx, "isbn", q, chunk, [None] * len(chunk)))
        idx += 1
    rows = conn.execute(
        """SELECT isbn13, oclc FROM plan_seed
           WHERE oclc IS NOT NULL AND oclc <> '' ORDER BY isbn13 ASC""").fetchall()
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        q = "(" + " OR ".join(f"oclc:{r['oclc']}" for r in chunk) + IA_QUERY_SUFFIX + ")"
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
    requests-style injectable ``get`` for tests.  IPv4-forced: the GB API key
    is IPv4-allowlisted and httpx happy-eyeballs picks IPv6 -> 403
    (2026-10-02: a 1,000-request trickle silently queried 0)."""
    import httpx
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(timeout=30, follow_redirects=True,
                      transport=transport) as client:
        return client.get(url, params=params)


def connect_ro(db: str | Path) -> sqlite3.Connection:
    """Read-only connection (M5.8 parallel executors: ia_plan is read, never
    written — no shared-DB writes during parallel execution)."""
    conn = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def execute_ia_element(conn: sqlite3.Connection, element_idx: int, *,
                       get=_http_get, sleep=time.sleep,
                       min_interval: float = IA_MIN_INTERVAL,
                       max_retries: int = IA_MAX_RETRIES,
                       results_dir: str | Path | None = None) -> dict:
    """Execute one ia_plan element with rate discipline (<=1 req/s,
    exponential backoff on 429/503, max ``max_retries``).  Deterministic +
    resumable: each result is upserted; reruns skip done elements and skip
    ISBNs already resolved by an earlier phase.

    M5.8 parallel mode: with ``results_dir`` set, NO DB writes happen —
    hits go to ``results_dir/slice_<idx>.jsonl`` (one ``{"isbn13": ...,
    "ia_identifier": ...}`` object per resolved ISBN) and completion is
    marked by ``results_dir/done_<idx>`` (the skip-if-done signal across
    reruns).  ``conn`` is only read (ia_plan / prior ht/wd resolution)."""
    row = conn.execute("SELECT * FROM ia_plan WHERE element_idx=?",
                       (element_idx,)).fetchone()
    if row is None:
        raise ValueError(f"no ia_plan element {element_idx}")
    results = Path(results_dir) if results_dir is not None else None
    if results is not None:
        if (results / f"done_{element_idx}").exists():
            return {"element": element_idx, "skipped_done": True, "hits": 0}
    if row["done"]:
        return {"element": element_idx, "skipped_done": True, "hits": 0}
    isbns: list[str] = json.loads(row["isbns"])
    keys: list = json.loads(row["keys"]) if row["keys"] else [None] * len(isbns)
    kind: str = row["kind"]

    live = [i13 for i13 in isbns if not _resolved(conn, i13)]
    hits = 0
    slice_lines: list[str] = []
    local_resolved: set[str] = set()

    def _record(i13: str, ident: str) -> None:
        nonlocal hits
        hits += 1
        if results is not None:
            slice_lines.append(
                json.dumps({"isbn13": i13, "ia_identifier": ident}))
            local_resolved.add(i13)
        else:
            conn.execute(
                "UPDATE enrich_status SET ia_identifier=?, ia_source='search' "
                "WHERE isbn13=? AND ia_identifier IS NULL",
                (ident, i13))
    if live:
        # Two-phase (2026-09-30 IA incident redesign): advancedsearch currently
        # omits isbn/oclc FIELDS from returned docs, so batch-query attribution
        # is impossible — but q= filtering and numFound remain authoritative.
        # Phase A probes each batch with rows=0 (numFound only); phase B
        # resolves ONLY matching batches per-key (identifier IS returned).
        probe = _request_json(
            get, {"q": row["query"], "rows": 0, "output": "json"},
            sleep, max_retries, min_interval)
        if probe is not None and int(
                probe.get("response", {}).get("numFound", 0) or 0) > 0:
            if kind == "isbn":
                pairs = [(i13, f"(isbn:{i13}) AND mediatype:texts")
                         for i13 in live]
            else:
                pairs = [(i13, f"(oclc:{k}) AND mediatype:texts")
                         for k, i13 in zip(keys, live) if k]
            for i13, single_q in pairs:
                if i13 in local_resolved or _resolved(conn, i13):
                    continue
                data = _request_json(
                    get, {"q": single_q, "rows": 1, "output": "json"},
                    sleep, max_retries, min_interval)
                docs = (data or {}).get("response", {}).get("docs", [])
                for d in docs:
                    if not isinstance(d, dict):
                        continue
                    ident = d.get("identifier")
                    if not ident:
                        continue
                    if IA_CONTAINER_RE.match(str(ident)):
                        continue   # donation pallet/container, not a scan
                    _record(i13, str(ident))
                    break
    if results is not None:
        results.mkdir(parents=True, exist_ok=True)
        (results / f"slice_{element_idx}.jsonl").write_text(
            "".join(line + "\n" for line in slice_lines), encoding="utf-8")
        (results / f"done_{element_idx}").write_text(now() + "\n",
                                                     encoding="utf-8")
        return {"element": element_idx, "skipped_done": False, "hits": hits}
    conn.execute("UPDATE ia_plan SET done=1, executed_at=? WHERE element_idx=?",
                 (now(), element_idx))
    conn.commit()
    _mark_checked(conn, "ia")
    return {"element": element_idx, "skipped_done": False, "hits": hits}


def merge_ia_results(conn: sqlite3.Connection,
                     results_dir: str | Path) -> dict:
    """M5.8 single-writer consolidation: stream every ``slice_*.jsonl``
    produced by parallel executors and apply
    ``UPDATE enrich_status SET ia_identifier=? WHERE isbn13=? AND
    ia_identifier IS NULL``.  Idempotent (already-set rows are skipped),
    unknown ISBNs are counted, never inserted.  Runs AFTER the array job,
    before assign-status (which needs sources_checked to include 'ia')."""
    ensure_schema(conn)
    applied = skipped = unknown = 0
    files = sorted(Path(results_dir).glob("slice_*.jsonl"))
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            i13, ident = obj.get("isbn13"), obj.get("ia_identifier")
            if not i13 or not ident:
                continue
            row = conn.execute(
                "SELECT ia_identifier FROM enrich_status WHERE isbn13=?",
                (i13,)).fetchone()
            if row is None:
                unknown += 1
            elif row["ia_identifier"] is not None:
                skipped += 1
            else:
                conn.execute(
                    "UPDATE enrich_status SET ia_identifier=? "
                    "WHERE isbn13=? AND ia_identifier IS NULL",
                    (str(ident), i13))
                applied += 1
    conn.commit()
    _mark_checked(conn, "ia")
    return {"files": len(files), "applied": applied,
            "skipped_already_set": skipped, "unknown_isbn": unknown}


def _resolved(conn: sqlite3.Connection, isbn13: str) -> bool:
    r = conn.execute(
        "SELECT ht_access, wd_fulltext, ia_identifier, pg_id, gallica_ark "
        "FROM enrich_status WHERE isbn13=?", (isbn13,)).fetchone()
    return bool(r and (r["ht_access"] == "allow" or r["wd_fulltext"] == 1
                       or r["ia_identifier"] or r["pg_id"]
                       or r["gallica_ark"]))


def _request_json(get, params: dict, sleep, max_retries: int,
                  min_interval: float = IA_MIN_INTERVAL,
                  url: str = IA_SEARCH_URL):
    import httpx
    attempt = 0
    while True:
        sleep(min_interval)          # rate discipline: <=1 req/s
        try:
            resp = get(url, params)
        except httpx.HTTPError:
            # transport-level failure (ConnectError 'connection reset by
            # peer', 2026-10-03 BnF run): same backoff as 429/503
            if attempt < max_retries - 1:
                sleep(2.0 ** attempt)
                attempt += 1
                continue
            return None              # caller treats as failed row, retried next run
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


# ------------------------------------------------- stage 3b (M5.5): ocaid sweep
def ocaid_sweep(conn: sqlite3.Connection, dump: str | Path,
                max_records: int | None = None,
                progress_every: int = 1_000_000) -> dict:
    """Cross-edition ocaid pass (SPEC-M5.5 Change 1): stream the editions
    dump ONCE; for records whose work (works[0]) is in the workset AND that
    carry a non-empty ``ia``, stage (work_key, ocaid, edition_key).  Then
    upgrade workset ISBNs whose ia_identifier is NULL to the work's ocaid
    (ia_source='ocaid') — assign-status then derives NT from the identifier.
    Deterministic single pass, idempotent upserts."""
    ensure_schema(conn)
    work_keys = {r["work_key"] for r in
                 conn.execute("SELECT work_key FROM enrich_workset")
                 if r["work_key"]}
    staged: list[tuple] = []
    n_read = 0

    def flush():
        if staged:
            conn.executemany(
                "INSERT OR REPLACE INTO ocaid_stage (work_key, ocaid, edition_key) "
                "VALUES (?,?,?)", staged)
            staged.clear()
            conn.commit()

    with m4.open_dump(path=dump) as fh:
        for key, obj in m4.iter_dump_records(fh, progress_every, "ocaid"):
            if key == "_summary" or not key.startswith("/books/"):
                continue
            if max_records is not None and n_read >= max_records:
                break
            n_read += 1
            works = [w.get("key") for w in obj.get("works") or []
                     if isinstance(w, dict)]
            work_key = works[0] if works else None
            if work_key is None or work_key not in work_keys:
                continue   # works outside the workset ignored
            # 2026 dump format: IA identifier lives in `ocaid` (primary) or
            # `ia_loaded_id`; there is NO `ia` field on edition records
            # (2026-10-02 finding — 56.7M-record pass staged 0 with `ia`).
            ias = [str(x) for x in _m4_list(obj.get("ocaid"))
                   or _m4_list(obj.get("ia_loaded_id")) if str(x).strip()]
            if not ias:
                continue
            staged.append((work_key, ias[0], key))
            if len(staged) >= BATCH:
                flush()
    flush()

    conn.execute(
        """UPDATE enrich_status SET
             ia_identifier = (SELECT o.ocaid FROM ocaid_stage o
                              JOIN enrich_workset w ON w.work_key = o.work_key
                              WHERE w.isbn13 = enrich_status.isbn13
                              ORDER BY o.edition_key ASC LIMIT 1),
             ia_source = 'ocaid'
           WHERE ia_identifier IS NULL
             AND EXISTS (SELECT 1 FROM enrich_workset w
                         JOIN ocaid_stage o ON o.work_key = w.work_key
                         WHERE w.isbn13 = enrich_status.isbn13)""")
    conn.commit()
    works_with = conn.execute(
        "SELECT COUNT(DISTINCT work_key) c FROM ocaid_stage").fetchone()["c"]
    upgraded = conn.execute(
        "SELECT COUNT(*) c FROM enrich_status "
        "WHERE ia_source='ocaid' AND ia_identifier IS NOT NULL").fetchone()["c"]
    return {"records_read": n_read, "works_with_ocaid": works_with,
            "upgraded": upgraded}


# ------------------------------------------------- stage 3c (M5.5): GB trickle
GB_VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"
GB_KEY_DEFAULT_FILES = (Path("/root/projects/lastcopy/secrets/gbooks.key"),)


def resolve_gb_key(key_file: str | Path | None = None) -> str:
    """--key-file > LASTCOPY_GBOOKS_KEY env > server secrets path >
    ~/.config/lastcopy/gbooks.key; KeyRequiredError when nothing resolves."""
    import os

    from .sources.gbooks import KEY_ENV, KEY_FILE, KeyRequiredError
    if key_file is not None:
        p = Path(key_file)
        if p.is_file():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        raise KeyRequiredError(f"no Google Books key at --key-file {p}")
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env
    for p in (*GB_KEY_DEFAULT_FILES, KEY_FILE):
        if p.is_file():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt
    raise KeyRequiredError(
        f"no Google Books key (tried {KEY_ENV}, "
        + ", ".join(str(p) for p in (*GB_KEY_DEFAULT_FILES, KEY_FILE)) + ")")


def _gb_classify(data: dict | None) -> tuple[str, str | None]:
    """Volumes response -> (gb_status, gb_identifier): 'none' (0 results),
    'metadata' (results, no full view), 'full' (any viewability FULL)."""
    items = (data or {}).get("items") or []
    if not items:
        return "none", None
    full = False
    ident = None
    for it in items:
        if not isinstance(it, dict):
            continue
        if ident is None and it.get("id"):
            ident = str(it["id"])
        vi = it.get("volumeInfo") or {}
        ai = it.get("accessInfo") or {}
        for v in (ai.get("viewability"), ai.get("accessViewStatus"),
                  vi.get("viewability"), vi.get("accessViewStatus")):
            if v and "FULL" in str(v):
                full = True
    return ("full" if full else "metadata"), ident


def gb_trickle(conn: sqlite3.Connection, budget: int, *,
               key_file: str | Path | None = None,
               get=_http_get, sleep=time.sleep,
               min_interval: float = IA_MIN_INTERVAL,
               max_retries: int = IA_MAX_RETRIES) -> dict:
    """Budgeted Google Books verification (SPEC-M5.5 Change 2): CR-first
    selection, ~1 req/s, 429/503 exponential backoff.  Skips rows with
    gb_status already set (resumable); stops immediately at budget."""
    from collections import Counter

    key = resolve_gb_key(key_file)   # KeyRequiredError when absent
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT s.isbn13 FROM enrich_status s
           JOIN enrich_workset w ON w.isbn13 = s.isbn13
           WHERE s.gb_status IS NULL
           ORDER BY CASE s.status WHEN 'CR' THEN 0 WHEN 'EN' THEN 1
                      WHEN 'VU' THEN 2 WHEN 'NT' THEN 3 ELSE 4 END,
                    w.score DESC, w.isbn13 ASC
           LIMIT ?""", (budget,)).fetchall()
    counts: Counter = Counter()
    for r in rows:
        data = _request_json(
            # 2026-10-03 A/B result (quota-reset test): GB's `isbn:` index
            # returns 0 results even for known books (P&P 9780141439518);
            # the PLAIN query returns 5. Use the bare ISBN.
            get, {"q": r["isbn13"], "key": key},
            sleep, max_retries, min_interval, url=GB_VOLUMES_URL)
        if data is None:
            continue   # request failed -> stays NULL, retried next run
        gb_status, gb_id = _gb_classify(data)
        conn.execute(
            "UPDATE enrich_status SET gb_status=?, gb_identifier=? WHERE isbn13=?",
            (gb_status, gb_id, r["isbn13"]))
        conn.commit()
        counts[gb_status] += 1
        if sum(counts.values()) >= budget:
            break
    return {"queried": sum(counts.values()), "counts": dict(counts)}


# ------------------------------------------------------------------ stage 4
def assign_status(conn: sqlite3.Connection) -> dict:
    """Stage 4: Book Red List rules (pure Python rules + SQL; no network).

    Precedence (SPEC-M5-ENRICHMENT "Stage 4"):
      1. digital_full (ht allow OR wd_fulltext=1 OR ia hit OR pg_id OR
         gallica_ark — M6 rescue sources) -> NT
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
                  s.pg_id, s.gallica_ark,
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
    out = {"assigned": len(updates), "counts": dict(counts)}
    out.update(assign_custody(conn))   # M5.6: chain custody (single entry point)
    return out


def _rowget(r, key):
    """Column access that tolerates plain-dict rule inputs (tests) missing
    newer columns."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _rule(r) -> tuple[str, str]:
    full_srcs = []
    if r["ht_access"] == "allow":
        full_srcs.append("ht allow")
    if r["wd_fulltext"] == 1:
        full_srcs.append("wd fulltext")
    if r["ia_identifier"]:
        full_srcs.append("ia hit")
    if _rowget(r, "pg_id"):
        full_srcs.append("pg hit")          # M6: Gutenberg rescue
    if _rowget(r, "gallica_ark"):
        full_srcs.append("gallica hit")     # M6: Gallica rescue
    if full_srcs:
        return "NT", f"full digital exists ({'+'.join(full_srcs)}); artifact may still be scarce"
    edition_count = r["edition_count"] or 1
    checked = {s for s in (r["sources_checked"] or "").split(",") if s}
    if "ia" not in checked:
        # verified-absent requires the IA pass to have actually covered this
        # row; books lacking OCLC are NOT un-verifiable (ISBN queries suffice)
        return "DD", "not verified: IA check never ran for this row (gap)"
    if edition_count == 1:
        return "CR", "edition_count=1; no digital in HT/IA/WD"
    if edition_count <= 3:
        return "EN", f"edition_count={edition_count}; no digital in HT/IA/WD"
    return "VU", f"edition_count={edition_count}; no digital in HT/IA/WD"


# ------------------------------------------------- stage 4b (M5.6): custody
def assign_custody(conn: sqlite3.Connection) -> dict:
    """M5.6: custody-quality tag, derived purely from existing evidence
    columns (no re-derivation, no network).  The acquisition queue ranks
    restricted-custody books above open-custody ones.

      open       — ht_access='allow' OR ia_identifier IS NOT NULL
                    (IA counts as open by default; the lending-collection
                    distinction is a future refinement) OR a PG/Gallica
                    rescue hit (M6: free digital)
      restricted — readable digital ONLY at Google ('gb_status='full'')
      none       — no readable digital found anywhere we checked
      unknown    — checks incomplete (e.g., IA never ran for the row)
    """
    ensure_schema(conn)
    from collections import Counter
    counts: Counter = Counter()
    rows = conn.execute(
        "SELECT isbn13, ht_access, ia_identifier, gb_status, pg_id, "
        "gallica_ark, sources_checked FROM enrich_status").fetchall()
    updates = []
    for r in rows:
        if (r["ht_access"] == "allow" or r["ia_identifier"] or r["pg_id"]
                or r["gallica_ark"]):
            custody = "open"   # definitive positive evidence, order-free
        else:
            checked = {s for s in (r["sources_checked"] or "").split(",") if s}
            if "ia" not in checked:
                custody = "unknown"
            elif r["gb_status"] == "full":
                custody = "restricted"
            else:   # gb_status NULL or in ('none','metadata')
                custody = "none"
        updates.append((custody, r["isbn13"]))
        counts[custody] += 1
    conn.executemany("UPDATE enrich_status SET custody=? WHERE isbn13=?",
                     updates)
    conn.commit()
    return {"custody_assigned": len(updates), "custody": dict(counts)}


# --------------------------------------------- M5.7: national-library holdings
# Physical-holdings evidence from legal-deposit national libraries
# (SPEC-M5.7-HOLDINGS).  Status/custody rules are UNTOUCHED this phase:
# holdings refine acquisition priority only.
HOLDINGS_INSTITUTIONS = ("bnf", "dnb", "loc", "ndl")

# Verified bulk sources (research/2026-10-02-national-library-holdings.md):
#   dnb — full MARC21-xml copy, 5 parts ~12.3GB / 37.2M records, anonymous
#   loc — MDSConnect BooksAll.2016, 43 parts ~3GB / 25M records (2016
#         snapshot; the SRU top-up covers post-2016), anonymous
#   ndl — JAPAN/MARC weekly ZIPs (small, anonymous); bnb has no bulk
#         bnf dumps are RDF — SRU-first, no bulk MARC set
HOLDINGS_BULK_URLS = {
    "dnb": [f"https://data.dnb.de/DNB/dnb_all_dnbmarc.{i}.mrc.xml.gz"
            for i in range(1, 6)],
    "loc": [f"https://www.loc.gov/cds/downloads/MDSConnect/"
            f"BooksAll.2016.part{n:02d}.xml.gz" for n in range(1, 44)],
    "ndl": "https://www.ndl.go.jp/file/data/data_service/jnb_product/"
           "jmo{week}.zip",
}


def fetch_holdings_bulk(institution: str, *, data_dir: str | Path | None = None,
                        parts: list[int] | None = None,
                        weeks: list[str] | None = None,
                        timeout: int = 0) -> dict:
    """Stage A: download bulk MARC21-xml sets under
    ``data/holdings/<inst>/``.  Resumable (curl -C -, IPv4-forced), each
    file gzip-integrity-checked; already-complete files are skipped.
    Download+verify only — no parsing (see ``parse_holdings_bulk``)."""
    import subprocess
    if institution not in ("dnb", "loc", "ndl"):
        raise ValueError(f"no bulk MARC set for '{institution}' "
                         "(bnf is SRU-first; bl blocked)")
    base = Path(data_dir) if data_dir is not None else Path("data/holdings")
    dest = base / institution
    dest.mkdir(parents=True, exist_ok=True)
    if institution == "ndl":
        if not weeks:
            raise ValueError("ndl bulk needs --weeks (ISO week numbers, "
                             "e.g. 202637)")
        urls = [HOLDINGS_BULK_URLS["ndl"].format(week=w) for w in weeks]
    elif institution == "dnb":
        urls = [HOLDINGS_BULK_URLS["dnb"][p - 1]
                for p in (parts or range(1, 6))]
    else:
        urls = [HOLDINGS_BULK_URLS["loc"][p - 1]
                for p in (parts or range(1, 44))]
    stats = {"institution": institution, "downloaded": 0, "resumed": 0,
             "skipped_ok": 0, "failed": 0, "files": []}
    for url in urls:
        target = dest / url.rsplit("/", 1)[-1]
        stats["files"].append(str(target))
        if target.exists() and _gzip_ok(target):
            stats["skipped_ok"] += 1
            continue
        existed = target.exists()
        rc = subprocess.call(
            ["curl", "-C", "-", "-4", "-L", "--fail", "--retry", "3",
             "-o", str(target), url], timeout=timeout or None)
        if rc == 0 and _gzip_ok(target):
            stats["resumed" if existed else "downloaded"] += 1
        else:
            stats["failed"] += 1
    return stats


def _gzip_ok(path: Path) -> bool:
    """Full-decompression gzip integrity check (truncated tails fail)."""
    import gzip as _gz
    try:
        with _gz.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _marc_streams(path: str | Path):
    """Yield decoded text streams of MARC21-xml payloads: plain .xml,
    .xml.gz, or members inside NDL JAPAN/MARC .zip archives.

    Shell-unexpanded glob patterns (CLI callers quoting e.g.
    '/path/BooksAll.2016.*.xml.gz') are expanded here (2026-10-03
    postmortem: a literal pattern silently parsed 0 records); a pattern
    matching nothing raises with the pattern in the message."""
    import glob as _glob
    import io
    import zipfile
    p = str(path)
    if any(c in p for c in "*?["):
        matches = sorted(_glob.glob(p))
        if not matches:
            raise FileNotFoundError(
                f"--file glob pattern matched nothing: {p}")
        for m in matches:
            yield from _marc_streams(m)
        return
    if p.lower().endswith(".zip"):
        with zipfile.ZipFile(p) as zf:
            for name in sorted(zf.namelist()):
                if not name.lower().endswith((".xml", ".xml.gz")):
                    continue
                raw = zf.open(name)
                if name.lower().endswith(".gz"):
                    import gzip as _gz
                    raw = _gz.open(raw, "rb")
                yield name, io.TextIOWrapper(raw, encoding="utf-8",
                                             errors="replace")
    elif p.endswith(".gz"):
        yield p, gzip.open(p, "rt", encoding="utf-8", errors="replace")
    else:
        yield p, open(p, "rt", encoding="utf-8", errors="replace")


def _iter_marc_records(stream):
    """RAM-bounded iterparse: yield each <record> element, clearing as we go
    (works for <collection> wraps and bare single-record docs, any namespace)."""
    import xml.etree.ElementTree as ET
    parser = ET.iterparse(stream, events=("start", "end"))
    root = None
    for event, elem in parser:
        if event == "start":
            if root is None:
                root = elem
        elif _local(elem.tag) == "record":
            yield elem
            elem.clear()
            if root is not None:
                root.clear()


def _marc_isbns(record) -> tuple[str | None, list[str]]:
    """(record_id from 001, ISBN strings from 020 $a) — qualifier suffixes
    like ' (hbk.)' stripped before normalization downstream."""
    rec_id: str | None = None
    isbns: list[str] = []
    for el in record.iter():
        t = _local(el.tag)
        if t == "controlfield" and el.get("tag") == "001" and el.text:
            rec_id = rec_id or el.text.strip()
        elif t == "datafield" and el.get("tag") == "020":
            for sf in el:
                if (_local(sf.tag) == "subfield" and sf.get("code") == "a"
                        and sf.text):
                    isbns.append(sf.text.split("(")[0].strip())
    return rec_id, isbns


def parse_holdings_bulk(conn: sqlite3.Connection, institution: str,
                        files: list[str | Path],
                        progress_every: int = 100_000) -> dict:
    """Stage A parse pass: stream MARC21-xml, extract 020 $a ISBNs
    (normalized via isbn.py, ISBN-10 and hyphenated-13 both fold to isbn13),
    match against the in-memory workset set, upsert holdings rows.
    RAM-bounded, progress lines, idempotent."""
    ensure_schema(conn)
    workset = {r["isbn13"]
               for r in conn.execute("SELECT isbn13 FROM enrich_workset")}
    n_read = matched = 0
    upserts: list[tuple] = []

    def flush():
        if upserts:
            conn.executemany(
                "INSERT OR REPLACE INTO holdings "
                "(isbn13, institution, record_id) VALUES (?,?,?)", upserts)
            upserts.clear()
            conn.commit()

    for path in files:
        for name, stream in _marc_streams(path):
            for record in _iter_marc_records(stream):
                n_read += 1
                if progress_every and n_read % progress_every == 0:
                    print(f"[holdings:{institution}] {n_read:,} records "
                          f"({matched:,} matched)", file=sys.stderr,
                          flush=True)
                rec_id, raws = _marc_isbns(record)
                for raw in raws:
                    norm = normalize_isbn(raw)
                    if norm and norm[0] in workset:
                        upserts.append((norm[0], institution, rec_id))
                        matched += 1
                if len(upserts) >= BATCH:
                    flush()
            stream.close()
    flush()
    rows = conn.execute(
        "SELECT COUNT(*) c FROM holdings WHERE institution=?",
        (institution,)).fetchone()["c"]
    return {"institution": institution, "records_read": n_read,
            "isbn_matches": matched, "holdings_rows": rows}


# SRU top-ups (Stage B).  All verified keyless 2026-10-02; query forms from
# the research notes (dnb isbn=, bnf 'bib.isbn all "..."', loc bath.isbn=,
# ndl isbn=).  loc lx2 serves plain HTTP on port 210 ONLY — TLS handshake
# fails there (unexpected-EOF), so http:// is deliberate, not an oversight.
SRU_ENDPOINTS = {
    "dnb": {"url": "https://services.dnb.de/sru/dnb", "version": "1.1",
            "query": "isbn={isbn}", "schema": "MARC21-xml"},
    "ndl": {"url": "https://ndlsearch.ndl.go.jp/api/sru", "version": None,
            "query": "isbn={isbn}", "schema": "dcndl_v3"},
    "bnf": {"url": "https://catalogue.bnf.fr/api/SRU", "version": "1.2",
            "query": 'bib.isbn all "{isbn}"', "schema": "unimarcxchange"},
    "loc": {"url": "http://lx2.loc.gov:210/lcdb", "version": "1.1",
            "query": "bath.isbn={isbn}", "schema": "mods"},
}
HOLDINGS_MIN_INTERVAL = 0.5     # <=2 rps sustained per host
HOLDINGS_MAX_RETRIES = 3        # exponential backoff on 429/503


def _sru_request(get, url: str, params: dict, sleep, max_retries: int,
                 min_interval: float):
    """SRU GET with the same discipline as _request_json: min-interval
    pacing, exponential backoff on 429/503; returns response text or None."""
    import httpx
    attempt = 0
    while True:
        sleep(min_interval)
        try:
            resp = get(url, params)
        except httpx.HTTPError:
            # transport failure mid-crawl (ReadError 'connection reset',
            # LOC 2026-10-03): same backoff as 429/503, then None
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


def _parse_sru(text: str) -> tuple[int, str | None]:
    """(numberOfRecords, recordId) from an SRU searchRetrieveResponse.
    Namespace-agnostic: SRU 1.1/1.2 wrappers differ; record ids live in
    MARC 001, dcndl identifiers, or MODS recordIdentifier."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return 0, None
    n = 0
    rec_id: str | None = None
    for el in root.iter():
        t = _local(el.tag)
        if t == "numberOfRecords" and el.text and el.text.strip().isdigit():
            n = int(el.text.strip())
        elif rec_id is None and el.text and el.text.strip() and (
                (t == "controlfield" and el.get("tag") == "001")
                or t == "recordIdentifier"):
            rec_id = el.text.strip()
    return n, rec_id


def enrich_holdings(conn: sqlite3.Connection, institution: str,
                    budget: int, *, get=None, sleep=time.sleep,
                    min_interval: float = HOLDINGS_MIN_INTERVAL,
                    max_retries: int = HOLDINGS_MAX_RETRIES) -> dict:
    """Stage B: SRU top-up per institution — ONLY workset ISBNs without a
    holdings row for that institution (post-bulk residual, or institutions
    without bulk).  IPv4-forced transport (DNB lacks IPv6), budget-capped,
    resumable (held rows are skipped on rerun).  National-scope zero hits
    are legitimate misses, not errors."""
    if institution not in SRU_ENDPOINTS:
        raise ValueError(f"unknown institution '{institution}' "
                         f"(want one of {','.join(sorted(SRU_ENDPOINTS))})")
    ensure_schema(conn)
    if get is None:
        get = _http_get        # resolved at call time (tests/CLI patch point)
    ep = SRU_ENDPOINTS[institution]
    rows = conn.execute(
        """SELECT w.isbn13 FROM enrich_workset w
           WHERE NOT EXISTS (SELECT 1 FROM holdings h
                             WHERE h.isbn13 = w.isbn13
                               AND h.institution = ?)
           ORDER BY w.isbn13 ASC LIMIT ?""",
        (institution, budget)).fetchall()
    held = missed = failed = 0
    for r in rows:
        params: dict = {"operation": "searchRetrieve",
                        "query": ep["query"].format(isbn=r["isbn13"]),
                        "maximumRecords": 1, "recordSchema": ep["schema"]}
        if ep["version"]:
            params["version"] = ep["version"]
        text = _sru_request(get, ep["url"], params, sleep, max_retries,
                            min_interval)
        if text is None:
            failed += 1          # stays rowless -> retried next run
            continue
        n, rec_id = _parse_sru(text)
        if n > 0:
            conn.execute("INSERT OR IGNORE INTO holdings "
                         "(isbn13, institution, record_id) VALUES (?,?,?)",
                         (r["isbn13"], institution, rec_id))
            conn.commit()
            held += 1
        else:
            missed += 1
    return {"institution": institution, "budget": budget,
            "queried": held + missed + failed, "held": held,
            "missed": missed, "failed": failed}


def assign_holdings_summary(conn: sqlite3.Connection) -> dict:
    """Fill ``enrich_status.holdings`` with comma-joined institution codes
    (derived, alphabetical); '' when none.  Same single pass derives
    ``custody_physical`` (M5.9): wild (no holdings, non-NT), single (exactly
    one institution), multi (two+), unheld-nt (status NT, no holdings).
    No effect on status/custody rules."""
    from collections import Counter
    ensure_schema(conn)
    conn.execute(
        """UPDATE enrich_status SET
             holdings = (
               SELECT COALESCE(GROUP_CONCAT(h.institution), '')
               FROM (SELECT institution FROM holdings h
                     WHERE h.isbn13 = enrich_status.isbn13
                     ORDER BY h.institution ASC) h),
             custody_physical = (
               CASE
                 WHEN (SELECT COUNT(DISTINCT h.institution) FROM holdings h
                       WHERE h.isbn13 = enrich_status.isbn13) >= 2 THEN 'multi'
                 WHEN (SELECT COUNT(DISTINCT h.institution) FROM holdings h
                       WHERE h.isbn13 = enrich_status.isbn13) = 1 THEN 'single'
                 WHEN status = 'NT' THEN 'unheld-nt'
                 ELSE 'wild'
               END)""")
    conn.commit()
    counts: Counter = Counter()
    phys: Counter = Counter()
    n_rows = n_any = 0
    for r in conn.execute("SELECT isbn13, holdings, custody_physical "
                          "FROM enrich_status"):
        n_rows += 1
        for c in (r["holdings"] or "").split(","):
            if c:
                counts[c] += 1
        if r["holdings"]:
            n_any += 1
        if r["custody_physical"]:
            phys[r["custody_physical"]] += 1
    return {"rows": n_rows, "with_holdings": n_any,
            "by_institution": dict(counts),
            "custody_physical": dict(phys)}


def custody_report(conn: sqlite3.Connection) -> dict:
    """M5.9 physical-custody buckets (Theory's directive: a single
    undigitized institutional copy is 'safe but not really safe'):
      safe-but-not-really-safe = custody='restricted' OR
                                  (status='CR' AND custody_physical='single')
      wild                     = status='CR' AND custody_physical='wild'
      captive-secure           = status='CR' AND custody_physical='multi'
    Pure SQL read, no rule changes."""
    ensure_schema(conn)

    def _n(where: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) c FROM enrich_status WHERE {where}"
        ).fetchone()["c"]

    return {"safe-but-not-really-safe":
            _n("custody='restricted' OR "
               "(status='CR' AND custody_physical='single')"),
            "wild": _n("status='CR' AND custody_physical='wild'"),
            "captive-secure":
            _n("status='CR' AND custody_physical='multi'")}


# ------------------------------------------------------------------ export
WORKSET_HEADER = ["isbn13", "title", "author", "year", "language",
                  "edition_count", "score", "status", "custody",
                  "custody_physical", "holdings",
                  "status_basis", "gb_status", "pg_id", "gallica_ark"]


def export_workset(conn: sqlite3.Connection, csv_path: str | Path,
                   md_path: str | Path | None = None) -> dict:
    """Export the workset WITH status columns, ordered by status severity
    (CR, EN, VU, NT, DD) then score DESC, isbn13 ASC.  The provisional note
    is dynamic (SPEC-M5.5 Change 3): kept only while any exported row still
    has gb_status NULL.  gb_status is the final column."""
    ensure_schema(conn)
    order = "CASE s.status WHEN 'CR' THEN 0 WHEN 'EN' THEN 1 WHEN 'VU' THEN 2 " \
            "WHEN 'NT' THEN 3 WHEN 'DD' THEN 4 ELSE 5 END"
    sql = f"""SELECT s.isbn13, c.title, c.year, c.language, w.edition_count,
                      w.score, s.status, s.custody, s.custody_physical,
                      s.holdings,
                      s.status_basis, s.gb_status, s.pg_id, s.gallica_ark,
                      wr.author_keys
              FROM enrich_workset w
              JOIN enrich_status s ON s.isbn13 = w.isbn13
              LEFT JOIN candidates c ON c.isbn13 = w.isbn13
              LEFT JOIN works_ref wr ON wr.work_key = w.work_key
              ORDER BY {order}, w.score DESC, w.isbn13 ASC"""
    rows = conn.execute(sql).fetchall()
    gb_pending = any(r["gb_status"] is None for r in rows)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(WORKSET_HEADER)
        for r in rows:
            wtr.writerow([r["isbn13"], r["title"],
                          m4.resolve_authors(conn, r["author_keys"]),
                          r["year"], r["language"], r["edition_count"],
                           r["score"], r["status"], r["custody"],
                           r["custody_physical"] or "",
                            r["holdings"] or "",
                            r["status_basis"], r["gb_status"],
                            r["pg_id"], r["gallica_ark"]])
        if gb_pending:
            wtr.writerow([f"# {PROVISIONAL_NOTE}"])
    if md_path:
        title = "lastcopy Book Red List — workset" + \
            (" (provisional)" if gb_pending else "")
        lines = [f"# {title}", "",
                 f"Top {len(rows)} enriched workset rows. "
                 + (f"{PROVISIONAL_NOTE.capitalize()}."
                    if gb_pending else "Google Books verification complete."),
                 "",
                  "| # | isbn13 | title | author | year | lang | eds | score "
                  "| status | custody | custody_physical | holdings "
                  "| status_basis | gb_status | pg_id | gallica_ark |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            title_ = str(r["title"] or "").replace("|", "\\|")
            author = m4.resolve_authors(conn, r["author_keys"]).replace("|", "\\|")
            lines.append(f"| {i} | {r['isbn13']} | {title_} | {author} "
                         f"| {r['year']} | {r['language'] or ''} "
                         f"| {r['edition_count']} | {r['score']} "
                          f"| {r['status']} | {r['custody'] or ''} "
                          f"| {r['custody_physical'] or ''} "
                          f"| {r['holdings'] or ''} "
                          f"| {r['status_basis']} "
                          f"| {r['gb_status'] or ''} "
                          f"| {r['pg_id'] or ''} "
                          f"| {r['gallica_ark'] or ''} |")
        if gb_pending:
            lines += ["", f"_{PROVISIONAL_NOTE}._", ""]
        else:
            lines += [""]
        Path(md_path).write_text("\n".join(lines), encoding="utf-8")
    return {"exported": len(rows), "csv": str(csv_path),
            "md": str(md_path or ""), "gb_pending": gb_pending}
