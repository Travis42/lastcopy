"""M4 slim equivalence oracle: slim path must reproduce the FAT-path golden
list byte-for-byte.

`docs/m4-list-top50.csv` was exported from the FAT schema (titles stored in
editions_ref) on the deterministic 10k fixture from
`scripts/m4_golden_fixture.py` (seed 20260929, commit cf13791). This test
rebuilds the same DB through the SLIM path — slim ingest-editions ->
ingest-works -> gen-candidates -> export-list with the fixture editions dump
for the one-pass title backfill — and asserts identical output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lastcopy import m4

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "docs" / "m4-list-top50.csv"


def test_slim_path_matches_golden_fat_list(tmp_path):
    dumps = tmp_path / "dumps"
    subprocess.run([sys.executable, str(REPO / "scripts" / "m4_golden_fixture.py"),
                    str(dumps), "10000"], check=True, cwd=str(REPO),
                   capture_output=True)
    conn = m4.connect(tmp_path / "slim.db")
    try:
        m4.ingest_works(conn, dumps / "ol_dump_works_latest.txt.gz",
                        dumps / "ol_dump_authors_latest.txt.gz", progress_every=0)
        m4.ingest_editions(conn, file=dumps / "ol_dump_editions_latest.txt.gz",
                           progress_every=0)
        m4.gen_candidates(conn, max_editions=1)
        out = tmp_path / "slim-top50.csv"
        stats = m4.export_list(conn, top=50, csv_path=out,
                               editions_dump=dumps / "ol_dump_editions_latest.txt.gz")
    finally:
        conn.close()
    assert stats["exported"] == 50
    assert stats["titles_backfilled"] == 50
    assert out.read_bytes() == GOLDEN.read_bytes()
