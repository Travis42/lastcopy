#!/usr/bin/env python3
"""Generate deterministic gzipped mini-dumps in the REAL OL dump TSV format
(type\tkey\trevision\tlast_modified\tJSON) for M4 golden runs.

Usage: m4_golden_fixture.py OUTDIR [N_EDITIONS]
Writes ol_dump_works_latest.txt.gz / ol_dump_authors_latest.txt.gz /
ol_dump_editions_latest.txt.gz — feed them to lastcopy ingest-works /
ingest-editions --file / gen-candidates / export-list (SPEC M4 item 6).
"""

from __future__ import annotations

import gzip
import json
import random
import sys
from pathlib import Path

from lastcopy.isbn import isbn10_to_13

TS = "2024-08-01T00:00:00.000000+00:00"
LANGS = ["eng", "eng", "eng", "eng", "por", "deu", "fra", "spa", "ita", None]


def isbn13(rng, used):
    while True:
        core = "978" + f"{rng.randrange(10 ** 9):09d}"
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
        i13 = core + str((10 - total % 10) % 10)
        if i13 not in used:
            used.add(i13)
            return i13


def line(type_, key, obj):
    return "\t".join([type_, key, "1", TS, json.dumps(obj, ensure_ascii=False)])


def main():
    outdir = Path(sys.argv[1])
    n_editions = int(sys.argv[2]) if len(sys.argv) > 2 else 10_000
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20260929)
    used: set[str] = set()

    works_lines, authors_lines, edition_lines = [], [], []
    w = 0
    while len(edition_lines) < n_editions:
        wkey, akey = f"/works/OL{w}W", f"/authors/OL{w}A"
        works_lines.append(line("/type/work", wkey, {
            "title": f"Work Number {w}", "authors": [{"key": akey}]}))
        authors_lines.append(line("/type/author", akey, {
            "name": rng.choice(["Ana Silva", "J. Doe", "Müller & Sons",
                                "_unicode_作者", "O’Néill"])}))
        # edition-count profile: most works 1-2 editions, some 5+
        n_eds = rng.choice([1, 1, 1, 1, 2, 2, 3, 5, 8])
        for e in range(n_eds):
            lang = rng.choice(LANGS)
            year = rng.choice([None, 1888, 1910, 1926, 1927, 1950, 1969, 1970,
                               1985, 2001, 2015])
            ed = {"title": f"Work Number {w} — edição {e}" if lang == "por"
                  else f"Work Number {w} Edition {e}",
                  "works": [{"key": wkey}],
                  "publishers": [rng.choice(["Arcádia", "Penguin", "Éditions du Seuil"])]}
            if year:
                ed["publish_date"] = rng.choice(
                    [str(year), f"March {year}", f"{year}-04-05"])
            if lang:
                ed["languages"] = [f"/languages/{lang}"]
            if rng.random() < 0.12:  # IA scan present -> excluded from candidates
                ed["ia"] = [f"worksNum{w:07d}{e}"]
            if rng.random() < 0.15:
                ed["oclc_numbers"] = [str(rng.randrange(10 ** 8))]
            roll = rng.random()
            if roll < 0.05:      # no ISBN at all -> dropped
                pass
            elif roll < 0.15:    # isbn10-only -> converted
                i10 = f"{rng.randrange(10 ** 9):09d}"
                tot = sum(int(c) * (10 - i) for i, c in enumerate(i10))
                chk = (11 - tot % 11) % 11
                i10 += "X" if chk == 10 else str(chk)
                ed["isbn_10"] = [i10]
                assert isbn10_to_13(i10)
            elif roll < 0.25:    # multi-isbn -> one row per valid isbn13
                ed["isbn_13"] = [isbn13(rng, used), isbn13(rng, used)]
            else:
                ed["isbn_13"] = [isbn13(rng, used)]
            edition_lines.append(line("/type/edition", f"/books/OL{w}M{e}", ed))
        w += 1
    edition_lines = edition_lines[:n_editions]

    rng.shuffle(edition_lines)
    for name, lines in [("ol_dump_works_latest.txt.gz", works_lines),
                        ("ol_dump_authors_latest.txt.gz", authors_lines),
                        ("ol_dump_editions_latest.txt.gz", edition_lines)]:
        with gzip.open(outdir / name, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"{outdir / name}: {len(lines):,} records")


if __name__ == "__main__":
    main()
