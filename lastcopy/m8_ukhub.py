"""M8.3 — Jisc Library Hub Discover Z39.50 harvester (SPEC-M8.3-UKHUB).

Runs ON APPRENTICE against the Library Hub Discover public Z39.50 face
(``tcp:3.250.189.53:210`` = z3950.libraryhub.jisc.ac.uk — IP pinned, the
lab's resolver fails on the hostname; the lab only needs an /etc/hosts
entry at import time, printed by ``ukhub-join``).

Engineering choice — yaz-client batch subprocess over PyZ3950 (live-checked
2026-10-09): /usr/bin/yaz-client 5.x is installed on apprentice, speaks
Z39.50 v3 to this YAZ-based target and, critically, negotiates the server's
diagnostic-suggested XML presentation (``format xml`` → MODS with the COPAC
holdings extension) which PyZ3950's record-syntax handling would have to be
hand-walked through and the package itself is long unmaintained.  One
yaz-client invocation per chunk of ISBNs keeps a *persistent* session
(single ``open``, then find/show per ISBN) so the connection setup cost is
amortised; pacing is enforced with an in-script ``sleep 1`` between ISBN
blocks (⩾ the 0.6 s min-interval) plus 0.6 s + jitter between invocations.

Per ISBN: PQF ``@attr 1=7 <isbn13>`` (use-attribute 7 = ISBN); fallback to
the plain term (also works) on the one retry allowed per transport error,
then ``status='err'``.  Hit count is always stored; records are presented
in XML and institution codes parsed from ``physicalLocation
authority="UkMaC"`` — if presentation carries a diagnostic (e.g. 238
"try XML instead" on some records) or parsing fails, the row degrades to
count-only (``institutions=''``, import then writes ``n=<count>``).

``ukhub-import`` (lab side) folds ukhub_matches into holdings
(institution 'jisc-uk', detail=institution codes or 'n=<count>') on the
m4.db scratch pattern, same as lobid-import.
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_HOST = "tcp:3.250.189.53:210"
HOSTS_HINT = ("3.250.189.53 z3950.libraryhub.jisc.ac.uk")
DEFAULT_WORKSET = "/mnt/HC_Volume_105940927/dumps/workset_cr.isbns"
CHUNK = 50                # ISBNs per persistent yaz-client session
SHOW_MAX = 25             # records presented per find (institution cap)
MIN_INTERVAL = 0.6        # s between queries (+ jitter)
JITTER_MAX = 0.4
PROGRESS_EVERY = 2_000

HITS_RE = re.compile(r"Number of hits: (\d+)")
CODE_RE = re.compile(r'<physicalLocation authority="UkMaC">([^<]+)</physicalLocation>')
DIAG_RE = re.compile(r"Record not available in requested syntax|\[238\]")


# ------------------------------------------------------------ script build
def build_pqf(isbn13: str) -> str:
    """PQF query, use-attribute 1=7 (ISBN)."""
    return f"@attr 1=7 {isbn13}"


def build_batch_script(isbns, host: str = DEFAULT_HOST, show_max: int = SHOW_MAX,
                       plain: bool = False) -> str:
    """yaz-client batch script: one persistent open, then per ISBN a
    find (PQF, or plain term for the fallback retry) + show of up to
    show_max records, with ``sleep 1`` pacing between ISBN blocks."""
    lines = [f"open {host}", "format xml"]
    for i, isbn in enumerate(isbns):
        if i:
            lines.append("sleep 1")          # >= MIN_INTERVAL between queries
        q = isbn if plain else build_pqf(isbn)
        lines += [f"find {q}", f"show 1+{show_max}"]
    lines.append("quit")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parsing
def parse_transcript(text: str, isbns) -> dict:
    """Map each queried isbn (in order) to {'n_hits': int|None, 'codes':
    set[str], 'diagnostic': bool}.  Positional: the i-th 'Sent
    searchRequest.' segment belongs to the i-th find.  n_hits None =
    transport/search failure (no hits line seen) — retry candidate."""
    segments = text.split("Sent searchRequest.")[1:]
    connected = "Connection accepted" in text
    out = {}
    for i, isbn in enumerate(isbns):
        seg = segments[i] if i < len(segments) else ""
        m = HITS_RE.search(seg)
        n_hits = int(m.group(1)) if m else None
        if not connected:
            n_hits = None
        out[isbn] = {"n_hits": n_hits,
                     "codes": set(CODE_RE.findall(seg)),
                     "diagnostic": bool(DIAG_RE.search(seg))}
    return out


# ---------------------------------------------------------------- transport
def run_script(script: str, timeout: int | None = None) -> str:
    """Feed a batch script to yaz-client on stdin, return its transcript."""
    proc = subprocess.run(["yaz-client"], input=script, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"yaz-client rc={proc.returncode}: "
                           f"{proc.stderr.strip()[:200]}")
    return proc.stdout


# ----------------------------------------------------------------- harvest
def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def harvest(workset_isbns, out_db: str | Path, host: str = DEFAULT_HOST,
            chunk: int = CHUNK, show_max: int = SHOW_MAX,
            runner=run_script, rng: random.Random | None = None,
            log_every: int = PROGRESS_EVERY) -> dict:
    """Query Library Hub per isbn13 into ukhub_matches (resumable: isbn13
    with an existing status='ok' row is skipped).  One retry per transport
    error (plain-term fallback), then status='err'."""
    rng = rng or random.Random()
    out = sqlite3.connect(out_db)
    out.execute("""CREATE TABLE IF NOT EXISTS ukhub_matches (
                     isbn13       TEXT PRIMARY KEY,
                     n_hits       INTEGER,
                     institutions TEXT,
                     status       TEXT)""")
    done = {r[0] for r in out.execute(
        "SELECT isbn13 FROM ukhub_matches WHERE status='ok'")}
    todo = [i for i in sorted(workset_isbns) if i not in done]
    t0 = time.monotonic()
    stats = {"queried": 0, "matched": 0, "errors": 0, "skipped": len(done)}
    last_call = 0.0

    def paced(script: str, timeout: int | None = None) -> str:
        nonlocal last_call
        wait = (MIN_INTERVAL + rng.random() * JITTER_MAX) \
            - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.monotonic()
        return runner(script, timeout=timeout)

    def store(isbn: str, n_hits: int, codes, status: str) -> None:
        out.execute("INSERT OR REPLACE INTO ukhub_matches "
                    "(isbn13, n_hits, institutions, status) VALUES (?,?,?,?)",
                    (isbn, n_hits, " ".join(sorted(codes)), status))

    def log() -> None:
        print(f"[ukhub-join] done={stats['queried'] + stats['skipped']:,} "
              f"matched={stats['matched']:,} errors={stats['errors']:,} "
              f"elapsed={time.monotonic() - t0:,.0f}s", file=sys.stderr,
              flush=True)

    for block in _chunks(todo, chunk):
        script = build_batch_script(block, host, show_max)
        timeout = max(60, len(block) * 15)
        try:
            results = parse_transcript(paced(script, timeout=timeout), block)
        except Exception:
            results = {i: {"n_hits": None, "codes": set(), "diagnostic": False}
                       for i in block}
        # one retry per transport error, plain-term fallback (single-isbn)
        for isbn in block:
            r = results[isbn]
            if r["n_hits"] is None:
                try:
                    retry = parse_transcript(
                        paced(build_batch_script([isbn], host, show_max,
                                                 plain=True), timeout=30),
                        [isbn])[isbn]
                except Exception:
                    retry = {"n_hits": None, "codes": set()}
                r = retry if retry["n_hits"] is not None else r
            stats["queried"] += 1
            if r["n_hits"] is None:
                stats["errors"] += 1
                store(isbn, 0, set(), "err")
            else:
                if r["n_hits"] > 0:
                    stats["matched"] += 1
                store(isbn, r["n_hits"], r["codes"], "ok")
            if stats["queried"] % log_every == 0:
                log()
        out.commit()
    log()
    out.close()
    stats["elapsed_s"] = round(time.monotonic() - t0, 1)
    return stats


# ------------------------------------------------------------- lab import
def import_matches(matches_db: str | Path, conn: sqlite3.Connection) -> dict:
    """Lab side: fold ukhub_matches rows into holdings (institution
    'jisc-uk', detail=space-joined UkMaC codes or 'n=<count>' when the
    harvest degraded to count-only) on the m4.db scratch pattern, then
    re-derive holdings/custody summary."""
    from . import m5

    src = sqlite3.connect(f"file:{matches_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    inserted = count_only = 0
    for r in src.execute("SELECT isbn13, n_hits, institutions FROM "
                         "ukhub_matches WHERE status='ok' AND n_hits > 0 "
                         "ORDER BY isbn13"):
        detail = r["institutions"] or f"n={r['n_hits']}"
        count_only += 0 if r["institutions"] else 1
        cur = conn.execute(
            "INSERT OR IGNORE INTO holdings (isbn13, institution, "
            "record_id, detail) VALUES (?,?,?,?)",
            (r["isbn13"], "jisc-uk", None, detail))
        inserted += cur.rowcount
    conn.commit()
    src.close()
    summary = m5.assign_holdings_summary(conn)
    return {"rows_read": inserted, "inserted": inserted,
            "count_only_detail": count_only,
            "holdings_summary": summary}


# -------------------------------------------------------------------- CLI
def load_workset(path: str | Path) -> set[str]:
    """One isbn13 per line; blank lines/comments skipped."""
    out: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            v = line.strip()
            if v and not v.startswith("#"):
                out.add(v)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ukhub-join",
        description="Harvest Jisc Library Hub Discover Z39.50 holdings "
                    "per workset ISBN (runs on Apprentice).")
    ap.add_argument("--workset", default=DEFAULT_WORKSET)
    ap.add_argument("--out", default="data/scanner/ukhub_matches.db")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="Z39.50 target (IP-pinned; lab DNS fails)")
    ap.add_argument("--chunk", type=int, default=CHUNK)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap workset size (smoke runs)")
    args = ap.parse_args(argv)
    workset = load_workset(args.workset)
    if args.limit is not None:
        workset = set(sorted(workset)[:args.limit])
    print(f"[ukhub-join] workset={len(workset):,} isbns host={args.host}",
          file=sys.stderr)
    stats = harvest(workset, args.out, host=args.host, chunk=args.chunk)
    print("[ukhub-join] done: "
          + " ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    print(f"[ukhub-join] manual step on lab: append to /etc/hosts -> "
          f"'{HOSTS_HINT}'", file=sys.stderr)
    return 0


def main_import(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ukhub-import",
        description="Fold shipped ukhub_matches.db rows into lab holdings "
                    "(institution 'jisc-uk') and re-derive custody.")
    ap.add_argument("--matches", default="data/scanner/ukhub_matches.db")
    ap.add_argument("--db", default="data/m4.db")
    args = ap.parse_args(argv)
    from . import m4

    conn = m4.connect(args.db)
    try:
        stats = import_matches(args.matches, conn)
    finally:
        conn.close()
    print("[ukhub-import] done: "
          + " ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
