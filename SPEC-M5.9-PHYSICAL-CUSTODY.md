# SPEC — M5.9: physical-custody axis — "safe but not really safe" (Theory 2026-10-03)

## Goal
Theory's directive: "if the only remaining copy is at one place and not digitized and
available, it's in a 'safe but not really safe' bucket." Generalize the GB-restricted
concept to PHYSICAL custody: a single institution holding a book is one renovation fire,
one deaccession, one budget cut from silence.

## Design
- New column `enrich_status.custody_physical TEXT`:
  - `'wild'` — no holdings rows at all (status CR context)
  - `'single'` — exactly ONE institution holds a copy
  - `'multi'` — two or more institutions
  - `'unheld-nt'` — status NT (digital exists) AND no holdings — physical custody unknown/irrelevant
- Derived in `assign_holdings_summary` (same single pass that fills `holdings`).
- Export: `custody_physical` column after `custody`.
- Reporting helper `custody_report(conn)` returning the bucket counts Theory asked for:
  - safe-but-not-really-safe = (custody='restricted') OR (status='CR' AND custody_physical='single')
  - wild = status='CR' AND custody_physical='wild'
  - captive-secure = status='CR' AND custody_physical='multi'
- CLI: `custody-report` subcommand printing the buckets; no status-rule changes.

## Tests
- wild/single/multi derivation from holdings rows (1 vs 2 institutions; dnb+loc combo)
- unheld-nt case; summary idempotency; export column; CLI smoke.

## Out of scope
- acquisition ranking weights (next phase); GB changes; holdings SRU parallelization
  (results-dir mode — separate spec if the serial crawl proves too slow).
