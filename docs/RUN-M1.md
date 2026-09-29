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

> **Correction 2026-09-29 (M3.1):** the original line below — "RED-UNVERIFIED:
> The Brothers Karamazov (Penguin Classics 2003), 9780140283334" — was a
> mislabel. `9780140283334` is *Lord of the Flies* (Penguin 1999), confirmed by
> both Open Library and Google Books. See "M3.1 sample-data verification pass"
> below: the row was relabeled and a real Karamazov edition added separately.

| class | title | isbn | rationale |
|---|---|---|---|
| RED-UNVERIFIED | Lord of the Flies (Penguin 1999) — previously mislabeled as Karamazov | 9780140283334 | no-surrogate+holdings-unknown |

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

## M3.1 sample-data verification pass (2026-09-29)

`samples/lots.csv` was agent-authored from memory; SPEC M3.1 ordered a
cross-check of every ISBN row against Open Library + Google Books titles
before wiring the GB source. Every row was enriched live (all 4 sources,
1 req/s/host, GB over IPv4 via the delivered key) and CSV labels compared to
source-side titles. Mislabels found and fixed:

| ISBN | old (wrong) label | verified title (OL + GB agree) | fix |
|---|---|---|---|
| 9780140283334 | The Brothers Karamazov | **Lord of the Flies** (Golding, Penguin 1999) | relabeled (the known bug) |
| 9780486417783 | Aesop's Fables | **The Call of the Wild** (Jack London, Dover) | relabeled |
| 9780553212419 | The Scarlet Letter | **The Adventures of Sherlock Holmes** (Bantam 1986) | relabeled |
| 9781853260001 | Heart of Darkness | **Pride and Prejudice** (Wordsworth) | relabeled |
| 9780140186390, 9780140283372, 9780142437230 | (blank) | East of Eden / Women in Love / Don Quixote | filled in |
| (new row) | — | **The Brothers Karamazov**, Pevear/Volokhonsky, FSG (9780374528379) | added to keep a real in-copyright Karamazov edition in the sample |

Notes:

- The prior M1 "RED: Karamazov" line above was really LotF all along — same ISBN,
  same classification, corrected title.
- The Karamazov slot-swap (9780374528379) was intended to keep a modern
  in-copyright RED-UNVERIFIED case, but live evidence says otherwise: IA hosts
  public "Better World Books" pallet scans of that edition, so it classifies
  **GREEN** per matrix row 1. The modern RED-UNVERIFIED case is still exercised —
  by the Lord of the Flies row (9780140283334: no surrogate on ol/ia/wd/gb).
- Per-source title disagreement is itself a signal (v2 fodder, not built): e.g.
  Wikidata still links 9780486417783 → Project Gutenberg's *Aesop's Fables*
  (ebooks/215) while OL/GB both say *The Call of the Wild* — the shipped
  Wikidata fixture keeps the recorded real (mis)link, and the row is GREEN
  either way.

### M3.1 live re-run (all four sources, GB key via IPv4 forcing)

```console
$ LASTCOPY_FORCE_IPV4=1 lastcopy --db live.db ingest --csv samples/lots.csv
ingested 24 rows: 21 ISBN-keyed, 3 bib-stub (0 invalid-ISBN fallbacks)

$ LASTCOPY_FORCE_IPV4=1 lastcopy --db live.db enrich   # gb auto-joins (key resolves)
enrich done: {'ok': 84}   # 21x ol,ia,wd,gb — all ok, GB answered over IPv4

$ lastcopy --db live.db classify
classified: GREEN=20, RED-UNVERIFIED=1, UNKNOWN=3

$ lastcopy --db live.db report --md report.md --csv report.csv
```

GB evidence rows in the report (surrogate/evidence URLs): The Great Gatsby,
Adventures of Huckleberry Finn, The Call of the Wild and Pride and Prejudice
(Wordsworth) carry `books.google.*` PARTIAL-preview links; 13 more rows have
GB NO_PAGES/zero-hit evidence recorded (viewability + source-side
title/author/year in `source_hits.evidence_json`). The refreshed generated
report is shipped at `docs/RUN-M1-report.md`.
