"""Regenerate the scanner findings CSV from a sightings DB — the same
path that produced docs/scanner/sightings_eng1.csv, now carrying the
APPROVED circulation-tier column (Theory 2026-10-09: "seems like a good
schema"): ``risk_tier`` directly after ``currency``, computed per book
from summed offerings (latest sighting per source) + min price via
lastcopy.m7_scanner.circulation_tier.  Tiers influence no scoring until
the calibration hand-check passes.

Run:  python scripts/findings.py --db data/scanner/sightings.db \
        --m4 lastcopy.db --out docs/scanner/sightings_eng1.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from lastcopy.m7_scanner import DEFAULT_SOURCES, circulation_tier, \
    market_aggregates

COLUMNS = ("isbn13", "title", "year", "score", "source", "n_results",
           "top_price", "currency", "risk_tier", "listing_title",
           "serp_url", "seen_at")


def write_findings(sight_db: str | Path, m4_db: str | Path,
                   out: str | Path) -> int:
    """sightings.db (+ m4 db ATTACHed for title/year/score) -> findings
    CSV rows; returns the row count written."""
    conn = sqlite3.connect(str(sight_db))
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS m4db", (str(m4_db),))
    agg = market_aggregates(conn)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in conn.execute(
                "SELECT s.isbn13, c.title, c.year, w.score, s.source, "
                "s.n_results, s.top_price, s.currency, s.listing_title, "
                "s.serp_url, s.seen_at FROM sightings s "
                "LEFT JOIN m4db.candidates c ON c.isbn13 = s.isbn13 "
                "LEFT JOIN m4db.enrich_workset w ON w.isbn13 = s.isbn13 "
                "ORDER BY s.isbn13, s.id"):
            a = agg.get(r["isbn13"], {})
            w.writerow((r["isbn13"], r["title"], r["year"], r["score"],
                        r["source"], r["n_results"], r["top_price"],
                        r["currency"],
                        circulation_tier(a.get("offerings"),
                                         min(a.get("prices", []),
                                             default=None)),
                        r["listing_title"], r["serp_url"], r["seen_at"]))
            n += 1
    conn.close()
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="findings",
        description="Regenerate the scanner findings CSV (sightings.db "
                    "+ m4 ATTACH) with the APPROVED circulation-tier "
                    "column risk_tier after currency.")
    p.add_argument("--db", default="data/scanner/sightings.db",
                   help="sightings DB path (read-only)")
    p.add_argument("--m4", default="lastcopy.db",
                   help="M4/M5 SQLite DB path, ATTACHed for title/year/"
                        "score")
    p.add_argument("--out", default="docs/scanner/sightings_eng1.csv",
                   help="output CSV path")
    args = p.parse_args(argv)
    n = write_findings(args.db, args.m4, args.out)
    print(f"[findings] {n} rows -> {args.out} "
          f"(risk_tier after currency, sources "
          f"{','.join(DEFAULT_SOURCES)})")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
