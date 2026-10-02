# SPEC — M5.5: ocaid cross-edition sweep + GB trickle runner (Theory greenlit 2026-10-01)

## Context
Audit (audit/REPORT.md): CR precision 67% — the false-CRs are modern reprint ISBNs whose
WORK's original edition carries an Open Library `ia` (ocaid) link to a free scan on another
edition record. Current pipeline only checks `ia` on the ISBN-edition itself (candidates
were selected for ia-empty). One dump pass fixes the class. GB trickle: 1,000 req/day
(slurm scrontab job) writes gb_status to start clearing the "provisional" flag.

## Change 1 — `ocaid-sweep` CLI (m5.py + cli.py)
New subcommand `ocaid-sweep --file DUMP [--max-records N]`:
- Load work_key set from enrich_workset (50k) into memory (small).
- Stream the editions dump ONCE (reuse the existing dump iteration path). For each record:
  - work_key = works[0] (same extraction as m4.extract_edition)
  - if work_key ∈ set AND record has non-empty `ia` (any element of _as_list(obj.get("ia"))):
    stage (work_key, ocaid=first ia value, edition_key) into new table
    `ocaid_stage(work_key TEXT, ocaid TEXT, edition_key TEXT)` — batched upserts,
    progress line every 1M records. `--max-records N` caps the stream for canary runs.
- After the pass: for each workset ISBN whose work_key has an ocaid AND whose
  enrich_status.ia_identifier IS NULL: set ia_identifier=<ocaid> and append to
  status_basis marker "ocaid (cross-edition OL link)" via a new column note —
  implementation: set ia_identifier and let assign-status derive NT; ALSO record the
  evidence: new column `enrich_status.ia_source TEXT` = 'search' (default for direct
  hits) vs 'ocaid' for these. Backfill existing rows to 'search'.
- Print summary: records read, works with ocaid, workset ISBNs upgraded.
- Deterministic, resumable not required (single pass, idempotent upserts).

## Change 2 — GB trickle runner `gb-trickle` CLI (m5.py + cli.py)
New subcommand `gb-trickle --budget N [--key-file PATH]`:
- Key default: `/root/projects/lastcopy/secrets/gbooks.key` (falls back to
  ~/.config/lastcopy/gbooks.key); KeyRequiredError if absent.
- Select up to N workset ISBNs WHERE gb_status IS NULL, ORDER BY status severity
  (CR first: CASE status WHEN 'CR' THEN 0 WHEN 'EN' THEN 1 WHEN 'VU' THEN 2 WHEN 'NT' THEN 3 ELSE 4 END),
  then score DESC, isbn13 ASC.
- For each: GET https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn13}&key=...
  (httpx, 30s timeout, 429/503 exponential backoff max 3, ~1 req/s discipline).
  Record gb_status: 'none' (0 results), 'metadata' (results, no full view), or
  'full' (any volumeInfo has accessViewStatus/viewability containing FULL) — store
  also gb_identifier (volume id) when present. New columns on enrich_status:
  `gb_status TEXT, gb_identifier TEXT`.
- IMPORTANT politeness: stop immediately when budget reached; print summary counts.
- Idempotent/resumable: skips rows with gb_status set.

## Change 3 — export note
export_workset: the provisional note becomes dynamic: "pending Google Books verification"
only while any sampled/exported row has gb_status NULL; if all have gb_status, drop the
provisional clause. Include gb_status as a final column in the workset CSV/MD.

## Tests
- ocaid-sweep: fixture dump with (a) work-edition ia-empty + (b) another edition of the
  SAME work with ia='scan123' → workset ISBN upgraded, ia_identifier='scan123',
  ia_source='ocaid'; --max-records canary cap honored; works outside set ignored.
- gb-trickle: stubbed httpx-shaped responses (0 results / preview-only / FULL) →
  gb_status none/metadata/full; budget respected; resume skips set rows; missing key raises.
- export note/column behavior.

## Out of scope
- HT re-verification of the 2 flagged NT rows (manual, separate); scoring weights;
  cross-edition TITLE matching beyond ocaid (v3 candidate).

## Runner notes (I build these, not OpenCode)
- Slurm job scripts: node-agnostic (no -w), bundle in the shared NFS tree, DB copied to
  node-local scratch at job start, copied back atomically at job end (single-writer).
- GB trickle: slurm scrontab entry, --exclude=wsl-gpu (home-IP rule), budget 1000.
