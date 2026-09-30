# M5 Accuracy Audit — 200-book random sample (2026-09-30/10-01)

Method: stratified random sample (seed 20260930) of the 50k Red-Listed workset —
150 CR + 50 NT. Four independent auditor agents verified each book against Google
Books, Internet Archive, HathiTrust, and general web, judging by ISBN first and
discovering titles from sources. Verdicts: CONFIRMED / REFUTED / UNCLEAR.
One auditor ISBN (9783622913587) was not in the sample — skipped as noise.

## Results

| Status | n  | Confirmed | Refuted | Unclear | Precision (of clear) |
|--------|----|-----------|---------|---------|----------------------|
| CR     | 149 | 78        | 39      | 32      | **67%**              |
| NT     | 50  | 45        | 3       | 2       | **94%**              |

## Findings

### False-CR pattern (39 of 117 clear CR verdicts)
All refuted CRs share one systematic cause: **edition-vs-work blindness**. The
workset is keyed to a modern reprint/microform ISBN (DG, RSC, Kessinger, CIHM
microfiche, dissertations reprints) while the underlying pre-1930 original is
freely readable — IA scan, HT full view, BSB/Google/LC scans, Gallica. The
pipeline's ISBN/OCLC matching cannot see a scan registered under the original
edition's identifiers. Examples:
- 9780781206389 (reprint) → 1875 Lippincott original on IA (lifeofbenjaminfr01fran)
- 9780742643604 (reprint) → Gardner, Dante and the Mystics (Dent 1913) full view on IA
- 9783111054582 (DG ISBN) → Heydemann 1834/35 Kategorien des Aristoteles, HT full view
- CIHM microfiche ISBNs whose works have ocaid-linked IA scans on OTHER editions

### NT contamination (3 of 48)
- 9782010007705 — auditor found only an Amazon listing (evidence weak; recheck)
- 9780856981739 / 9783486583533 — HT records exist but not full view (search-only)
  → these two look like ht_access classification errors to re-verify

### Unclear (32 CR + 2 NT)
Paywalled/lending-only/thin-metadata rows. Not false — unverifiable by web audit.

## Fix plan (v2 enrichment — priority order)
1. **OL ocaid sweep (cheapest, high yield)**: one streaming pass over the
   editions dump collecting `ia` (ocaid) per WORK_KEY (not per ISBN-edition);
   any work with a linked scan on ANY edition → digital exists (NT). Directly
   kills the CIHM/reprint class. ~20 min compute.
2. **Cross-edition title/author/year match** against IA/HT for remaining CRs
   (pre-1930 first). ~1 request per CR, canary'd array as before.
3. Re-verify the 3 refuted NT rows manually; reclassify as needed.
4. Re-run assign-status; expected CR precision ≥ 85-90% after (1)+(2).

## Artifacts
- sample.csv (200 rows, seeded) · verdicts-batch-{1..4}.tsv · summary-batch-{1..4}.md
- spotcheck-10.md — Theory's 10-row packet with evidence links
