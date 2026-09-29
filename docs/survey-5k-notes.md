# Survey 5k — reviewer notes (2026-09-29, Apprentice)

**Status: INTERIM — complete pending GB backfill.**

## What happened
- Run completed 14:52 UTC+0, rc=0, watchdog clean (12 heartbeats, zero stalls).
- Sample: 4,152 works (1900–2019). IA pass: 4,152/4,152 ok.
- Google Books free-tier daily quota (1,000/day) exhausted ~5 min into the GB pass:
  1,015 ok / 3,137 unavailable. Rows with an IA miss + gb-unavailable classify UNKNOWN
  by design (never silently GREEN/RED): 2,348 rows.
- **Decided rows: 1,804.** All percentages in `survey-5k.md` are over decided rows
  (Wilson CIs reflect n=1,804). The unknowns are NOT missing data — they are queued,
  re-checkable, and time-ordered (first ~40% of the run got full 2-source checks).

## Backfill plan (quota resets ~08:00–09:00 local 2026-09-30)
1. M3.3 adds `enrich --retry-unavailable` (re-enqueue unavailable source_hits).
2. Re-run enrich on /tmp/lc-survey5k.db → classify → regenerate report + rows CSV.
3. Re-verify counts, commit final artifacts, close this note.

## Interpretation caveats (for anyone citing these numbers)
- "No accessible digital surrogate" = found by NEITHER Internet Archive NOR Google
  Books. Wikidata full-text links and HathiTrust custodial copies were not consulted
  in survey mode → the true no-surrogate-anywhere rate is ≤ these figures.
- GB's catalog skews anglocentric; part of the non-English gap may be GB not listing
  a work IA also lacks. The per-row CSV records sources_checked for audit.
- era:pre-1927 cell is thin (n=25) — random draw of ISBN-keyed works under-samples
  the pre-ISBN era by construction; the bib-stub lane (D1) exists for exactly that gap.
- edition_count from Open Library is a work-level rarity proxy, not a holdings count.

## Reviewer correction (2026-09-29 17:50)
The M3.3 spec's `origin_note` diagnosis was wrong: the reviewer's empty-query came
from JOINing `classification` — survey mode classifies in memory and writes the
rows CSV without persisting to the classification table (0 rows; by design, the
CSV is the artifact). OpenCode nonetheless found and fixed a real latent bug:
`upsert_edition` clobbered non-empty `origin_note` on provenance-less re-upsert.
M3.3 verified: 101/101 tests, dry-count {'gb': 3137}, 429-cache eviction required
(cached error bodies would otherwise replay forever). Backfill armed for 2026-09-30 09:30.
