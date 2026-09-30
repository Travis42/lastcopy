# Batch 3 audit — rows 102-151 (data rows 101-150), all status=CR

1. Verdicts: CONFIRMED 30 · REFUTED 15 · UNCLEAR 5 (per-status: CR 30/15/5; NT n/a — zero NT rows in batch).
2. Pattern of the 15 refutations: all are POD/reprint ISBNs (De Gruyter, Reprint Services Corp, Kessinger) of pre-1930 originals whose scans sit freely on IA, HT (full view), Gallica, BSB, ГПИБ or Google Books — the pipeline's ISBN-first matching missed ISBN-less digital originals.
3. Systemic metadata defects found: bogus years (10 rows carry 1900/1913/1918/1920 placeholders for 20th-21st-c. works, e.g. 2014 play, 1988 Japanese architecture), and at least one wrong author (9780781278423 = Bret Harte, not Henry Fielding) + wrong language (9780674729995 = English, marked ger).
4. The 5 UNCLEAR: IA holds restricted/lending-only scans for 4 (developingcompre0000thom, newlivingapaces0000unse, britishinternati0000beac, spatmittelalter0000moel) and one unidentifiable title ("Achte Klasse", no author).
5. Note: Google Books API was quota-blocked (429) throughout; Google Books coverage was reconstructed via Brave search + books.google.com/books links — worth re-checking refutable leftovers (e.g. 978362994883 Forner) if the API recovers.
