# SPEC — M8: union-catalog holdings (K10plus first, SUDOC second) — 2026-10-09

## Context (research: clawd/research/2026-10-09-union-catalogs.md, live-verified)
Theory directive: use library-alliance catalogs, bulk-first where possible.
- K10plus GVK SRU (sru.k10plus.de/gvk): free, no key, `query=pica.isb=<isbn>`, verified
  live from lab with per-library holdings in PICA responses. BUILD FIRST. No dump →
  polite SRU harvest (the LOC pattern).
- SUDOC SRU: endpoint live but 5xx "temporary system error" today; retry at run; if
  still down, fallback = ABES datadumps (UNIMARC + localisations; phase 2 task).
- lobid: 0 hits on both probes (calibrate before use) + unreachable from lab → deferred.
- CERL HPB: pre-1830 scope → excluded from ISBN phase.
- WorldCat: full subscription only — parked (retention-commitment mapping noted for
  future custody modeling if institutional access ever appears).

## Design
### 1. `enrich-holdings --institution k10plus` (extend m5 holdings SRU family)
- Endpoint: https://sru.k10plus.de/gvk — version 1.1, recordSchema picaxml.
- Query per workset ISBN: `pica.isb=<isbn13>`; parse hits; per response extract the
  set of holding institution codes (PICA XML `<lib>`-style location elements — verify
  exact field at build from a real response, fixture it).
- Store: holdings table rows (institution='k10plus', held=1, detail=space-joined
  library codes, e.g. "DE-6 DE-38") — same shape as national rows. Budget-capped,
  resumable (skip ISBNs with existing k10plus rows), 0.5s min-interval, IPv4,
  transport-retry discipline (same as _sru_request).
### 2. Custody re-derivation after harvest: assign-holdings-summary must now count
  k10plus in holdings presence (wild→single/multi reclassifications) — verify
  custody_physical logic counts DISTINCT institutions including k10plus.
### 3. CLI `union-holdings-report`: wild/single/multi deltas attributable to k10plus
  (books whose only holding is k10plus, i.e. rescued by academic/state libraries).
### 4. Slurm job `job-m8-k10plus.sbatch`: scratch-copy → enrich k10plus (budget 50000)
  → assign-holdings-summary → custody-report → export+lists → write-back.
  ~7h at 0.5s/req. Pin -w lab,lab3 (lab2 down).

## Tests
- PICA XML fixture (real response excerpt incl. 2 library locations) → parser test
- holdings row shape incl. detail codes; resume-skip; budget stop
- custody re-derivation with k10plus-only holdings (wild→single)
- report deltas; CLI smoke with fake get.

## Out of scope
SUDOC retry (fold in as second institution when endpoint recovers — same pattern),
lobid calibration, WorldCat, CERL.
