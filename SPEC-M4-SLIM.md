# SPEC — M4 slim ingest (Theory-approved 2026-09-30, "slim")

## Context
- Repo: this tree (`/root/projects/lastcopy`, git). Heavy runs execute on **lab**; this tree is the code+test home (light I/O only).
- Problem: `editions_ref` stores 10 columns × ~56M ISBN-keyed editions (~2.2 KB/row → 100 GB+ DB). Downstream, only per-work edition COUNTS and the top-10k winners' TITLES are ever used. The CSV header is `isbn13,title,author,year,language,edition_count,score,rationale` — publishers/OCLC/ISBN-10 are never used anywhere downstream (verified: `gen_candidates`, `export_list`, `resolve_authors`).
- Approval: Theory approved slim-only storage 2026-09-30. The verified editions dump stays archived on lab for future re-derivation. Do NOT add a "fat" compat mode — slim is the only path.

## Changes — `lastcopy/m4.py`

1. **Slim `editions_ref` schema** (update `_SCHEMA` DDL):
   `(isbn13 TEXT PRIMARY KEY, edition_key TEXT, work_key TEXT, year INTEGER, language TEXT, ia TEXT)`
   — drop `isbn10`, `title`, `publishers`, `oclc_numbers`.
2. **`extract_edition`**: emit slim rows. Keep EXACT current semantics for isbn13 generation (multi-ISBN, ISBN-10 conversion, per-record dedupe), `work_key`, `year`, `language`, `ia`, `edition_key`. Remove title/isbn10/publishers/oclc extraction.
3. **`ingest_editions`**: INSERT only the slim columns. Batch size, retry, progress, `_backfill_edition_counts` behavior unchanged (COUNT(DISTINCT edition_key) is unaffected by slimming).
4. **`gen_candidates`**: unchanged EXCEPT `candidates.title` is inserted as NULL (no title in editions_ref). Do not change filters, scoring, ordering, or the candidates schema (title column stays, now nullable).
5. **NEW title backfill, integrated into `export_list`**:
   - Signature: `export_list(conn, top, csv_path, md_path=None, editions_dump: str | Path | None = None)`.
   - After selecting the top rows (ORDER BY score DESC, isbn13 ASC LIMIT ? — unchanged), if `editions_dump` is provided: stream the editions dump ONCE using the existing dump iteration path, and for any record whose generated ISBN-13 set intersects the winners' isbn13 set (in-memory set of ≤ top ISBNs), fill `title` using the EXACT current precedence: `obj.get("title") or obj.get("full_title") or obj.get("subtitle")`.
   - First matching record wins per ISBN (same value the fat path stored, since every row from one record carried that record's title).
   - Write the filled titles back: `UPDATE candidates SET title=? WHERE isbn13=?` for winners, then use them for CSV/MD exactly as now.
   - Winner ISBN not found in the dump → title stays NULL, CSV/MD emit empty (as today for missing titles). No crash.
6. **CLI (`lastcopy/cli.py`)**: `export-list` gains `--editions-dump PATH` (default None). None → old behavior with NULL titles (no backfill pass).

## Equivalence requirement (the oracle)
- The repo contains a golden FAT-path fixture artifact: `docs/m4-list-top50.csv` (+ fixture generator in tests, see commit cf13791 and `docs/M4-RUN.md`).
- NEW test `tests/test_m4_slim_equivalence.py`: build the same fixture DB through the SLIM path (slim ingest-editions → ingest-works → gen-candidates → export-list with `editions_dump=<fixture editions dump>`), then assert the output CSV equals `docs/m4-list-top50.csv` byte-for-byte.

## Tests (add/adjust; all must pass: `.venv/bin/python -m pytest tests/ -q`)
- Slim schema: `editions_ref` column set == expected; ingest no longer stores dropped fields.
- Backfill: multi-ISBN record → title filled for each matched winner ISBN; precedence chain title→full_title→subtitle exercised; winner absent from dump → NULL, no crash; backfill writes UPDATE into candidates.
- CLI: `--editions-dump` parses; omitted → titles NULL, export still succeeds.
- Update existing tests that referenced dropped columns; do not delete coverage — retarget it.

## Out of scope (must not change)
- Scoring weights, gen_candidates filters/SQL shape, export ordering, works/authors ingest, `_backfill_edition_counts`, candidates schema, WAL/pragmas.

## Deliverables
- Code + tests committed (conventional commit). One-paragraph note appended to `docs/M4-RUN.md` documenting the slim path and the retained-dump re-derivation story.
