"""M8.2 — lobid dump joiner (SPEC-M8.2-LOBID-JOIN, bulk-first).

Runs ON APPRENTICE against the 22 GB lobid-resources JSONL.gz dump
(~25M titles, 40M items, CC0): single streaming pass, constant memory,
matching records against the CR workset ISBN file; matched holdings rows
land in a small sqlite db that ships to lab.  ``lobid-import`` (lab side)
folds those rows into holdings (institution 'lobid', detail=space-joined
library ISIL codes) on the m4.db scratch pattern and re-derives custody
alongside the k10plus results.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

from .isbn import normalize_isbn

DEFAULT_WORKSET = "/mnt/HC_Volume_105940927/dumps/workset_cr.isbns"
DEFAULT_DUMP = "/mnt/HC_Volume_105940927/dumps/lobid-resources_2026-10-04.jsonl.gz"
PROGRESS_EVERY = 2_000_000


# ------------------------------------------------------------------ reading
class _RecordStream:
    """Iterable over the dump's JSONL records; bad (unparseable/non-dict)
    lines are counted in ``bad_lines`` and skipped."""

    def __init__(self, gzip_path: str | Path):
        self.path = gzip_path
        self.bad_lines = 0

    def __iter__(self):
        self.bad_lines = 0
        with gzip.open(self.path, "rt", encoding="utf-8",
                       errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    self.bad_lines += 1
                    continue
                if not isinstance(rec, dict):
                    self.bad_lines += 1
                    continue
                yield rec


def iter_records(gzip_path: str | Path) -> _RecordStream:
    """Stream parsed JSONL records from a .gz dump (constant memory);
    skip + count bad lines via ``stream.bad_lines`` after iteration."""
    return _RecordStream(gzip_path)


# ----------------------------------------------------------------- extract
def _library_code(held_by) -> str | None:
    """heldBy dict -> short code: isil, else last path segment of id, else
    label.  Real lobid records carry isil (e.g. 'DE-Kn28'); id fallback
    strips the trailing '#!' URI fragment."""
    if not isinstance(held_by, dict):
        return None
    isil = held_by.get("isil")
    if isinstance(isil, str) and isil.strip():
        return isil.strip()
    rid = held_by.get("id")
    if isinstance(rid, str) and rid.strip():
        seg = rid.rstrip("#!").rstrip("/").rsplit("/", 1)[-1]
        if seg:
            return seg
    label = held_by.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return None


def extract(record: dict) -> tuple[set[str], set[str]] | None:
    """(isbn13 set, library-code set) for a lobid record; None when the
    record has no holdings (bib-only — no custody value for M8.2)."""
    items = record.get("hasItem")
    if not isinstance(items, list) or not items:
        return None
    libraries: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _library_code(item.get("heldBy"))
        if code:
            libraries.add(code)
    if not libraries:
        return None
    isbns: set[str] = set()
    for raw in record.get("isbn") or []:
        norm = normalize_isbn(str(raw))
        if norm:
            isbns.add(norm[0])
    return isbns, libraries


# -------------------------------------------------------------------- join
def load_workset(path: str | Path) -> set[str]:
    """One isbn13 per line; blank lines/comments skipped."""
    out: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            v = line.strip()
            if v and not v.startswith("#"):
                out.add(v)
    return out


def join_dump(gzip_path: str | Path, workset_isbns: set[str],
              out_db: str | Path, progress_every: int = PROGRESS_EVERY) -> dict:
    """Single pass over the dump; every record with isbns ∩ workset gets
    one row per matched isbn13 in lobid_matches.  Constant memory."""
    out = sqlite3.connect(out_db)
    out.execute("""CREATE TABLE IF NOT EXISTS lobid_matches (
                     isbn13   TEXT NOT NULL,
                     libraries TEXT,
                     n_libs   INTEGER,
                     title    TEXT,
                     lobid_id TEXT)""")
    t0 = time.monotonic()
    lines = matched_recs = 0
    dist = {"1": 0, "2": 0, "3+": 0}
    for rec in iter_records(gzip_path):
        lines += 1
        if lines % progress_every == 0:
            print(f"[lobid-join] lines={lines:,} matched={matched_recs:,} "
                  f"elapsed={time.monotonic() - t0:,.0f}s", file=sys.stderr,
                  flush=True)
        got = extract(rec)
        if got is None:
            continue
        isbns, libraries = got
        hit = isbns & workset_isbns
        if not hit:
            continue
        matched_recs += 1
        libs = " ".join(sorted(libraries))
        n_libs = len(libraries)
        dist["1" if n_libs == 1 else "2" if n_libs == 2 else "3+"] += 1
        title = rec.get("title")
        lobid_id = rec.get("hbzId") or rec.get("id")
        for isbn13 in sorted(hit):
            out.execute(
                "INSERT INTO lobid_matches (isbn13, libraries, n_libs, "
                "title, lobid_id) VALUES (?,?,?,?,?)",
                (isbn13, libs, n_libs, title, lobid_id))
        out.commit()
    unique = out.execute(
        "SELECT COUNT(DISTINCT isbn13) FROM lobid_matches").fetchone()[0]
    out.close()
    return {"lines": lines, "matched_records": matched_recs,
            "unique_isbns": unique, "n_libs_distribution": dist,
            "elapsed_s": round(time.monotonic() - t0, 1)}


# ------------------------------------------------------------- lab import
def import_matches(matches_db: str | Path, conn: sqlite3.Connection) -> dict:
    """Lab side: fold lobid_matches rows into holdings (institution
    'lobid', record_id=lobid_id, detail=space-joined library codes) on
    the m4.db scratch pattern, then re-derive holdings/custody summary."""
    from . import m5

    src = sqlite3.connect(f"file:{matches_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    inserted = 0
    for r in src.execute("SELECT isbn13, libraries, lobid_id FROM "
                         "lobid_matches ORDER BY isbn13"):
        cur = conn.execute(
            "INSERT OR IGNORE INTO holdings (isbn13, institution, "
            "record_id, detail) VALUES (?,?,?,?)",
            (r["isbn13"], "lobid", r["lobid_id"], r["libraries"]))
        inserted += cur.rowcount
    conn.commit()
    src.close()
    summary = m5.assign_holdings_summary(conn)
    return {"rows_read": inserted, "inserted": inserted,
            "holdings_summary": summary}


# -------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="lobid-join",
        description="Stream the lobid-resources dump and join against the "
                    "CR workset ISBN file (runs on Apprentice).")
    ap.add_argument("--dump", default=DEFAULT_DUMP)
    ap.add_argument("--workset", default=DEFAULT_WORKSET)
    ap.add_argument("--out", default="lobid_matches.db")
    args = ap.parse_args(argv)
    workset = load_workset(args.workset)
    print(f"[lobid-join] workset={len(workset):,} isbns dump={args.dump}",
          file=sys.stderr)
    stats = join_dump(args.dump, workset, args.out)
    print("[lobid-join] done: "
          + " ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


def main_import(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="lobid-import",
        description="Fold shipped lobid_matches.db rows into lab holdings "
                    "(institution 'lobid') and re-derive custody.")
    ap.add_argument("--matches", default="data/scanner/lobid_matches.db")
    ap.add_argument("--db", default="data/m4.db")
    args = ap.parse_args(argv)
    from . import m4

    conn = m4.connect(args.db)
    try:
        stats = import_matches(args.matches, conn)
    finally:
        conn.close()
    print("[lobid-import] done: "
          + " ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
