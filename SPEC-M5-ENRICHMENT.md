# SPEC — M5: bulk enrichment + Book Red List classification (Theory-approved 2026-09-30)

## Goal
For the top-50k M4 candidates (deterministic: `ORDER BY score DESC, isbn13 ASC LIMIT 50000`
from the corrected candidates table), determine digital availability from BULK sources
first, then assign IUCN-style conservation statuses per the approved Book Red List.
Google Books per-record verification is a LATER, separate phase (Apprentice egress) —
statuses are provisional until then and must say so.

## New tables (m4.db)
- `enrich_workset (isbn13 TEXT PRIMARY KEY, work_key, edition_count, score, oclc, created_at)`
  — materialized top-50k + OCLC backfill (see stage 0).
- `enrich_status (isbn13 TEXT PRIMARY KEY, ht_access TEXT, ia_identifier TEXT,
  wd_fulltext INTEGER, gb_status TEXT, sources_checked TEXT, checked_at TEXT,
  status TEXT, status_basis TEXT)`
  — one row per workset ISBN. `gb_status` stays NULL until the GB phase.
  `status` ∈ {CR, EN, VU, NT, DD}; `status_basis` = short human-readable evidence line,
  e.g. `"edition_count=1; no digital in HT/IA/WD"`.

## Stage 0 — OCLC backfill (slim schema dropped oclc; re-derive from dump)
- New CLI `backfill-oclc --file DUMP --db DB`: stream the editions dump ONCE (same
  iteration path as `_backfill_titles`), for records whose ISBN-13s intersect the workset,
  store `oclc_numbers` (first value) into `enrich_workset.oclc`. Same first-record-wins rule.

## Stage 1 — HathiTrust hathifiles join (zero API)
- New CLI `enrich-ht --hathifile PATH --db DB`: stream-parse the TSV (columns per
  github.com/hathitrust/hathifiles README: htid, access, rights, …, oclc_num, isbn, …).
  Normalize ISBN-10/13 (reuse `lastcopy/isbn.py`); match workset ISBNs on ANY of the
  comma-joined ISBNs, else on OCLC when present. Store `ht_access` = the BEST (most open)
  row per ISBN: allow > deny; among allows keep first. Batch upserts, RAM-bounded
  (two-pass: build temp table of hathifile (norm_isbn, oclc, access) keyed by isbn —
  ~18M rows, then one SQL upsert join; do NOT hold 18M rows in a Python dict).

## Stage 2 — Wikidata full-text links (one SPARQL query, snapshot file)
- New CLI `enrich-wikidata --db DB --results PATH`: parse the saved SPARQL JSON result
  (fetched once by the pipeline runner, file on disk — the CLI itself stays offline/
  deterministic). `wd_fulltext` = 1 when the ISBN has a full-text work link.

## Stage 3 — Internet Archive batched search (the only API phase in this bundle)
- New CLI `enrich-ia --db DB --batch 30 --arraysize N`: generate the query plan —
  workset ISBNs not already matched (ht allow OR wd_fulltext), grouped 30 per
  advancedsearch OR-query; write plan rows (element_idx, query, isbn list) to
  `ia_plan` table. Execute mode: given element index(es), run the queries with
  rate discipline (≤1 req/s, exponential backoff on 429/503, max 3 retries),
  record `ia_identifier` on hits. Second sub-pass: OCLC-based OR-queries for still-
  unresolved rows that have OCLC.
- Deterministic + resumable: each query result upserted; reruns skip done rows.

## Stage 4 — status assignment (pure SQL + Python rules; no network)
New CLI `assign-status --db DB`. Rules (edition_count from workset; digital_full =
ht_access='allow' OR wd_fulltext=1 OR ia_identifier NOT NULL; digital_partial =
row present in HT with access='deny' OR ia hit with no full text marker —
conservative: partial only via HT deny for now):
- digital_full → **NT** (basis "full digital exists; artifact may still be scarce")
- else edition_count=1 → **CR**
- else edition_count 2–3 → **EN**
- else edition_count ≥4 → **VU**
- no signals resolvable (missing from all checks AND no OCLC AND not IA-queryable)
  → **DD**
All statuses get `status_basis` strings. EW/EX reserved for future market/holdings
phases (document in code comment only). Append a note to exported lists:
"provisional pending Google Books verification".

## Export
- Extend `export-list` with `--workset` flag: export the workset WITH status columns
  (isbn13,title,author,year,language,edition_count,score,status,status_basis)
  ordered by status severity (CR,EN,VU,NT,DD) then score DESC, isbn13 ASC.
  Default behavior unchanged.

## Tests (all green: `.venv/bin/python -m pytest tests/ -q`)
- Mini hathifile fixture (10 lines, mixed isbn formats + oclc joins + allow/deny
  precedence) → correct ht_access per ISBN.
- OCLC backfill: multi-ISBN record → oclc stored once, first-record-wins.
- IA plan generation: exactly ceil(n/30) elements, deterministic order; fake-response
  executor test (stub HTTP shape faithfully — requests-style response object) records
  hits and skips done rows on rerun.
- Status rules: table-driven test covering every rule incl. precedence and DD.
- export-list --workset: header + ordering + provisional note.

## Runner + slurm bundle (separate — I will create the bundle; code stays CLI-clean)
Pipeline runner script orchestrates: stage 0 → 1 → 2 (SPARQL fetch once) → plan →
IA array submit. NO sbatch/sbatch logic inside lastcopy code.

## Out of scope
- Google Books phase (Apprentice, later, separate spec); market/holdings checks (EW);
  scoring-weight changes; M4 schema changes.
