# SPEC — M6: digital-rescue sources (Gutenberg + Gallica/BnF bulk) — 2026-10-03

## Goal
Rescue false-CRs: books already digitized in Project Gutenberg or Gallica must leave
the endangered list (reclassified NT, custody open). Verified-endpoints research:
research/2026-10-03-m6-rescue-sources.md. Europeana/Trove deferred (keys pending;
ISBN search broken at both — title/author queries, lower yield).

## Phase A — Project Gutenberg (keyless, bulk-first)
- `fetch-gutenberg` CLI: download `pg_catalog.csv` (21.2MB, ~90k rows) to
  data/gutenberg/, resumable curl, integrity = parseable CSV + row-count print.
- `enrich-gutenberg` CLI: match workset against PG. PG has NO ISBNs — match on
  (normalized title, first-author-surname, year±2). THEN resolve ambiguous matches
  via Gutendex `https://gutendex.com/books/?isbn=<workset-isbn>` (no key) —
  a Gutendex ISBN hit is authoritative rescue. Mark enrich_status: new column
  `pg_id`; NT reclass via existing status logic (add PG to the digital-checks set:
  sources_checked += 'pg', NT if pg_id present).

## Phase B — Gallica/BnF digitized-books (keyless, two-OAI harvest + join)
- All live BnF ISBN APIs verified BROKEN (0 hits incl. their own examples) — bulk only.
- `fetch-gallica` CLI (stage-checkpointed, resumable by OAI resumptionToken):
  - Harvest 1: `oai.bnf.fr/oai2/OAIHandler` set `gallica` (OAI-NUM, 7.43M docs,
    ~123MB oai_dc) → extract gallica-ark → cb-ark pairs (dc:relation)
  - Harvest 2: `catoai.bnf.fr/oai2/OAIHandler` set `catalogue:edition:livres`
    (OAI-CAT) → extract cb-ark → ISBN (dc records carry ISBNs, verified)
  - ≤1 rps, browser UA (server 403s default curl UA), IPv4, gzip store,
    per-10k-records checkpoint files.
- `parse-gallica` CLI: offline join gallica-ark→cb-ark→ISBN against workset →
  `gallica_ark` column; NT reclass + custody open (Gallica = free digital).
- Runner: slurm stage jobs (fetch-NUM, fetch-CAT, join) — ~35h total harvest,
  checkpointed every step per decomposable-or-parallel doctrine; slices of the
  harvest MAY parallelize later via OAI segments if server allows.

## Rules
- Status rules: PG and Gallica hits => NT (open custody) — same semantics as IA/HT.
- No post-build patches; all derived columns land via assign-status chain.
- Books found => removed from wild/single buckets automatically on next slice.

## Tests
- CSV fixture for pg_catalog parsing + title/author matcher (normalization, ±2yr,
  surname-only) + Gutendex stub (shape: {count, results:[...]}).
- OAI XML fixtures (resumptionToken loop, dc:relation ark pair, dc ISBN) for both
  harvests; join logic (ark chains); workset match; NT reclass integration;
  idempotent re-harvest (skip via checkpoint); UA requirement (stub asserts header).

## Out of scope
- Europeana (needs Travis key + title/author query strategy), Trove (key risk),
- Wikisource/DOAB (wave 4), WorldCat (gated).
