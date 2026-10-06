# SPEC — M6.1: workset title backfill (pre-enrich) — 2026-10-06

## Incident (context)
Job lc-m6join 14827: `enrich-gutenberg` was a **silent no-op** — `78,282 PG rows vs
0 workset rows → pg_id set on 0`. Root-cause chain (all verified on lab):

1. M4-slim dropped titles from `editions_ref`; `candidates.title` is NULL for all
   19,488,788 rows. Titles only exist in the OL editions dump stream.
2. `enrich_gutenberg` todo query guards `WHERE … c.title IS NOT NULL` → scanned 0 rows.
3. The only title backfill (`m4._backfill_titles`) lives in the **top-N export**
   (`export-list` WITHOUT `--workset`), i.e. step 4/4, AFTER enrich runs — wrong order.
4. `export-list --workset` (the path M6 jobs actually use) **silently ignores**
   `--editions-dump` — `cmd_export_list` returns before ever touching it.
5. Job 14768 (`job-m6-pg.sbatch`) never got that far: it died on
   `enrich-gutenberg: error: --catalog required`.

Empirical probe (lab, full-stream scan, 2026-10-06): every workset ISBN originates
from this same dump (`extract_edition` reads isbn_13/isbn_10), so a dedicated pass
MUST recover titles. Interim at 35M/56.7M records: 31,666/50,000 matched,
**zero** matched-without-title. Full ~50k coverage expected. Probe script:
`/tmp/lcprobe/dumpscan.py` on lab (diagnostic only, not shipped).

## Deliverables

### A. `lastcopy/m6.py` — `backfill_titles(conn, editions_dump, *, progress_every=1_000_000) -> dict`
- Stream the editions dump ONCE (`m4.open_dump`, `m4.iter_dump_records`; reuse
  `m4._as_list`, `lastcopy.isbn.normalize_isbn`).
- Target set: `SELECT isbn13 FROM enrich_workset` (50k rows — NOT all 19.5M candidates).
- Semantics (match `m4._backfill_titles` exactly):
  - skip `_summary` + non-`/books/` keys;
  - title precedence `title or full_title or subtitle`; records with no title are
    skipped entirely (never clobber an existing non-NULL candidates.title);
  - ISBN extraction from `isbn_13` + `isbn_10` (10→13 conversion via normalize_isbn),
    first-match-wins per ISBN13;
  - only fill `candidates.title IS NULL` rows (stable + idempotent on reruns).
- Batched `executemany` UPDATE + periodic `commit()` (follow existing BATCH patterns).
- Return stats dict: `{"records_read", "workset_isbns", "titles_filled", "previously_filled"}`
  where `previously_filled` = count of workset isbns whose candidates.title was
  already non-NULL at entry (cheap SQL count).

### B. `lastcopy/cli.py` — new subcommand `backfill-titles`
- `--editions-dump` (required): path to `ol_dump_editions_latest.txt.gz`.
- `--progress-every` (int, default 1_000_000, 0 disables) passed through.
- Prints one line: `backfill-titles: {records_read:,} dump records → {titles_filled:,} titles filled ({previously_filled:,} already set) for {workset_isbns:,} workset rows`.

### C. `lastcopy/m6.py` — fail-loud guard in `enrich_gutenberg`
After building `todo`: if `rows scanned == 0` AND unchecked workset rows
(`enrich_status.pg_id IS NULL`) > 0 → `raise RuntimeError("enrich-gutenberg: 0 rows
scanned but N workset rows unchecked — candidates.title is NULL for the workset; run
backfill-titles --editions-dump first")`. Case todo==0 BECAUSE everything is already
checked (idempotent rerun) must NOT raise — distinguish via the unchecked count.
`cmd_enrich_gutenberg` catches the RuntimeError, prints `error: …` to stderr, rc 1.

### D. `lastcopy/cli.py` — close the silent-ignore hole in `cmd_export_list`
`export-list --workset --editions-dump X` currently ignores X. Make it an error:
print `error: --editions-dump is ignored by the workset export (titles come from
candidates.title — run backfill-titles first)` to stderr, rc 2.

### E. Tests — extend `tests/test_m6.py` (or new `tests/test_m61_backfill.py`)
Build fixture dump(s) as real gzipped TSV files in tmp_path (`type\tkey\trev\tts\tJSON`
lines — copy the pattern from existing m4 dump fixtures).
- fills title from isbn_13 record;
- isbn_10-only record matched via conversion;
- record with no title does not clobber an already-set title (pre-set one row);
- non-workset ISBN ignored; multi-ISBN record fills both rows;
- rerun after fill = no change, `titles_filled == 0`, `previously_filled` reflects;
- malformed line (bad JSON) skipped, no crash;
- guard: `enrich_gutenberg` raises when 0 titles + unchecked rows; does NOT raise
  when all rows already checked (stub catalog + no-op get);
- CLI: `backfill-titles --help` renders (extend test_help_renders list);
  `export-list --workset --editions-dump x.csv` exits rc 2.

## Non-goals
- No schema changes; no match_pg changes; gutendex budget unchanged (5000).
- No hathi/other title sources (evaluate coverage after the real run first).
- Job-script reordering happens ops-side on lab (not in this repo commit).

## Verification (OpenCode)
`python3 -m pytest tests/ -q` — full suite green, new tests included.
Smoke: build tiny DB via existing fixtures, run `backfill-titles` CLI end-to-end.

## Real-run plan (ops-side, after merge)
lab `job-m6-join.sbatch`: insert step 1/5 `backfill-titles --editions-dump $DATA/ol_dump_editions_latest.txt.gz`
before `enrich-gutenberg` (renumber echoes); same fix for `job-m6-pg.sbatch`.
~20 min stream at 56.7M records, fits the 3h budget. Rerun via sbx (ack required —
14827's failure class), rearm `m6-join-check` watcher.
