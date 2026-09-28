# M1 end-to-end run (documented)

Date: 2026-09-28 · Environment: Python 3.12, keyless sources only (OL + IA + Wikidata)
· DB: `lastcopy.db` (SQLite, WAL) · all requests polite: 1 req/s/host + jitter,
UA `lastcopy/0.1 (+https://github.com/lastcopy)`, responses cached (TTL 30 d).

## Commands and actual output

```console
$ pip install -e .
...

$ lastcopy ingest --csv samples/lots.csv
ingested 23 rows: 20 ISBN-keyed, 3 bib-stub (0 invalid-ISBN fallbacks)

$ lastcopy enrich
enrich:ol: 100%|██████████| 20/20 [02:19<00:00,  6.97s/ed]
enrich:ia: 100%|██████████| 20/20 [00:56<00:00,  2.85s/ed]
enrich:wd: 100%|██████████| 20/20 [04:47<00:00, 14.38s/ed]
enrich done: {'ok': 60} (queue resumable; cache TTL 30d)   # ~8 min wall clock, all 60 checks ok

$ lastcopy classify
classified: GREEN=19, RED-UNVERIFIED=1, UNKNOWN=3

$ lastcopy report --md docs/RUN-M1-report.md --csv report.csv
```

## Resulting classes (23 editions)

| class | count | reading |
|---|---|---|
| GREEN | 19 | public/lending surrogate found (IA scans, Project Gutenberg via Wikidata) |
| RED-UNVERIFIED | 1 | no surrogate found, holdings unknown (M1 keyless default) → confirmation queue |
| UNKNOWN | 3 | pre-ISBN bib-stub rows (decision D1: queued, not resolved in v1) |

### RED list (from the generated report)

| class | title | isbn | rationale |
|---|---|---|---|
| RED-UNVERIFIED | The Brothers Karamazov (Penguin Classics 2003) | 9780140283334 | no-surrogate+holdings-unknown |

Every GREEN entry carries evidence URLs, e.g.:

- Aesop's Fables (9780486417783) → `https://gutenberg.org/ebooks/215` (Wikidata → PG)
- Frankenstein (9780486282114) → `https://archive.org/details/frankenstein00shel` (IA scan)
- The Great Gatsby (9780743273565) → `https://archive.org/details/greatgat00fitz` (IA scan)

The full generated report is shipped at `docs/RUN-M1-report.md` (RED list first,
roll-up per work, counts by class).

## Notes

- **M2 stretch also landed:** `lastcopy --db survey.db survey --sample 15 --from 1930
  --to 1950` produced `all: n=15, no-surrogate 9 (60.0%), Wilson 95% CI [35.7%, 80.2%]`
  (see `docs/survey-sample.md`).

- "To Kill a Mockingbird" came out GREEN via an IA **lending** scan
  (`tokillmockingbir0000leeh_m6b3`) — per the matrix row 1, an IA lending scan is a
  public surrogate: a copy survives and is accessible (controlled digital lending),
  so it is not last-copy at risk.
- The 3 pre-ISBN rows (Raul Brandão, Camões, Nemésio — the Theory shelf use case)
  land as `bib-stub` queue entries classified UNKNOWN: no guessing (D1).
- Modern bestseller rows generally classify GREEN here because IA hosts lending
  scans for current in-print books; expect true RED-UNVERIFIED density on the
  obscure/pre-ISBN tail of real lots (e.g. the Karamazov row above, where no
  matched scan exists).
- Re-running `lastcopy enrich` after this run is near-instant for unchanged rows
  (cache) and picks up only unfinished queue items (crash-safe resume).
