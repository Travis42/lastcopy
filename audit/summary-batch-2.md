# Audit Summary — Batch 2 (rows 52–101, data rows 51–100, all status=CR)

1. Verdicts: 28 CONFIRMED · 14 REFUTED · 8 UNCLEAR (refutation rate 28%; every REFUTED is a freely readable full copy with URL in verdicts-batch-2.tsv).
2. REFUTED rows (52,53,54,61,62,64,65,76,85,88,90,91,92,100) are all reprint/microform ISBNs of PD works whose originals are open on IA/BSB/Google — CR was materially false for these.
3. Top pipeline misses: (a) OpenLibrary `ocaid` not consulted — 0665 CIHM fiche ISBNs 9780665413087/9780665451836 resolve to IA items cihm_41308/cihm_45183; (b) ISBN-13-only IA search misses 10-digit/CIHM identifiers; (c) title-level IA/BSB scans ignored for reprint ISBNs.
4. UNCLEAR (56,66,74,81,84,89,99,101): digital exists but purged (56), paywalled Springer/De Gruyter, or IA print-disabled/lending-only; CONFIRMED skew modern in-copyright works (55,63,73,78,79,96,98) plus bogus-year noise (68,72,83 = Lulu/Prentice/Houghton items with fake "1900").
5. Tooling caveats: Google Books API quota-exhausted (viewability inferred from PD date + verified open IA mirrors); catalog.hathitrust.org HTML search Cloudflare-blocked (used bib API only, empty for all sampled ISBNs); Springer/De Gruyter 403s prevented paywall confirmation on 3 rows.
