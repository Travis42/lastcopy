# M4 — THE LIST: offline candidate generator — golden run (2026-09-29)

Golden verification run on a deterministic 10k-record fixture (SPEC M4 item 6).
**Not the real dumps** — the 9.2G live stream is launched by Apprentice after
review. Fixture generated with `scripts/m4_golden_fixture.py` (seed 20260929),
gzipped mini-dumps in the real OL dump TSV format
(`type\tkey\trevision\tlast_modified\tJSON`).

## Commands (exact)

```
python scripts/m4_golden_fixture.py dumps 10000
lastcopy --db gold.db ingest-works   --data-dir dumps
lastcopy --db gold.db ingest-editions --file dumps/ol_dump_editions_latest.txt.gz
lastcopy --db gold.db gen-candidates --max-editions 1
lastcopy --db gold.db export-list --top 10000 --csv list.csv --md list.md
```

## Counts

| stage | count |
|---|---|
| works dump records | 3,749 |
| authors dump records | 3,749 |
| editions dump records | 10,000 (10,000 parsed, 0 bad) |
| ISBN-keyed `editions_ref` rows kept | 10,473 (multi-ISBN records fan out; ~5% records have no ISBN and drop) |
| works with edition_count = 1 | 1,649 |
| candidates (IA-empty ∧ edition_count ≤ 1) | 1,611 |
| exported list rows | 1,611 (top 10,000 cap not reached) |

Score histogram (max 8 = editions=1 + non-eng + pre-1927):
`8:199  7:247  6:256  5:542  4:92  3:275`
Language mix of candidates: eng 629 / por 180 / fra 161 / ita 163 / spa 145 /
deu 138 / unknown 195.

Top-50 sample of the list: `docs/m4-list-top50.csv` (CC0).

## Determinism

`gen-candidates` + `export-list` re-run on the same store produces a
byte-identical `list.csv` (`diff` empty). Scores are a pure function of
(edition_count, language, year); ordering ties break on isbn13 ascending.

## RAM bound

`/usr/bin/time -v` on `ingest-editions` over the 10k fixture: **peak RSS
44,020 KB (~43 MB)** — the 1.5G ceiling has ~35x headroom; nothing is
buffered, rows flush to SQLite every 10k. The suite also asserts a 512 MB
budget over a 50k-record fixture in a child process (`ru_maxrss`).

## Stream restart (documented failure mode)

The editions dump is never written to disk (`curl -sSL <url> | gunzip | parse`
straight into SQLite). If the gzip stream dies mid-way (truncation → EOFError,
or curl nonzero exit), `ingest-editions` restarts the whole stream from zero,
up to `--retries` (default 3). This is safe because `editions_ref` upserts are
idempotent (PK isbn13) and per-work edition counting is a post-stream SQL
`GROUP BY` backfill — a restart can never double-count. Retries exhausted →
nonzero exit with `StreamFailed`.

## Apprentice real-run checklist (post-review)

1. Download works + authors dumps into `data/` (HC volume symlink).
2. `lastcopy --db data/m4.db ingest-works --data-dir data/`
3. `lastcopy --db data/m4.db ingest-editions --stream-url https://openlibrary.org/data/ol_dump_editions_latest.txt.gz`
4. `lastcopy --db data/m4.db gen-candidates --max-editions 1`
5. `lastcopy --db data/m4.db export-list --top 10000 --csv data/list-top10k.csv --md data/list-top10k.md`
6. Verification waves: `lastcopy --db reg.db ingest --csv data/list-top10k.csv` (unchanged registry path).
