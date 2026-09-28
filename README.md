# lastcopy

**Last-copy registry prototype.** AI bulk-buyers purchase books ISBN-keyed, blind to
scarcity, then shred. No public artifact answers: *"which editions have no accessible
digital surrogate and few surviving copies?"* This is that artifact.

Paste a bulk-lot ISBN list; get a **RED list** — editions where the world could actually
lose the last accessible copy — before you ship the pallet.

## Classification matrix

| Condition | Class |
|---|---|
| Public surrogate exists (IA public scan, IA lending scan, Google Books FULL, Project Gutenberg, Standard Ebooks) | **GREEN** |
| Custodial-only surrogate (HathiTrust search-only) | **AMBER** |
| No surrogate + OCLC holdings ≤ 5 | **RED** |
| No surrogate + holdings unknown / for-sale ≤ 2 | **RED-UNVERIFIED** (confirmation queue) |
| No data yet / source down | **UNKNOWN** (never silently GREEN) |

Every classification carries a machine-readable rationale and evidence URLs.

## Quickstart (M1 — keyless: Open Library + Internet Archive + Wikidata)

```bash
pip install -e .
lastcopy ingest  --csv samples/lots.csv   # ISBN rows; pre-ISBN rows become bib-stubs
lastcopy enrich                          # polite async checks (1 req/s/host, cached)
lastcopy classify                        # apply the matrix
lastcopy report --md out.md --csv out.csv # RED list first + roll-up + counts
```

- Politeness: 1 request/second/host with jitter, User-Agent `lastcopy/0.1 (+https://github.com/lastcopy)`,
  every response cached (TTL 30 days), crash-safe resumable queue in SQLite.
- Sources down/quota-exhausted → `status=unavailable`, classification stays UNKNOWN.
- Pre-ISBN rows are ingested as `bib-stub` work keys (sha1 of normalized
  `title|author|year`) and queued — no fuzzy resolution in v1 (decision D1: keep
  ambiguity visible, never guessed).
- Google Books / HathiTrust / OCLC land in M3 behind a key-signup checklist; their
  source modules are scaffolded and raise clear "key required" errors today.

## Why this exists

Reporting by [404 Media](https://www.404media.co/) (e.g. its July 2026 investigation
"Company Offering Printed Books to Train AI Stops After 404 Media Report", on ISBNdb
offering to source up to a million printed books for AI training) and
[Futurism](https://futurism.com/) (its coverage of bulk buyers shredding rare books
for AI training data) shows AI companies bulk-buying used books for training data and
destroying the copies. Rare, out-of-print, foreign-language, and pre-ISBN books get
pulped alongside the millionth paperback because purchase orders are keyed to ISBNs
and structurally blind to scarcity. `lastcopy` cross-references candidate editions
against digital-surrogate existence (and, later, library holdings) and emits the
shortlist the world might actually lose.

## Status

- **M1 (done):** keyless OL + IA + Wikidata chain end-to-end, RED-list CSV/MD report,
  full test suite (`python3 -m pytest tests/ -q`).
- **M2 (stretch):** `lastcopy survey` — random OL sample → "% no accessible digital
  surrogate" + Wilson CI, split pre/post 1927.
- **M3 (later):** Google Books / HathiTrust / OCLC keys wired; confirm workflow;
  public dashboard.

See `SPEC.md` for the approved contract and `docs/RUN-M1.md` for a documented
end-to-end run.

## License

- Code: MIT (see `LICENSE`).
- Generated registry outputs (reports, CSVs, database dumps): CC0 (see `NOTICE`).
