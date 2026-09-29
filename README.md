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

## Quickstart (M1 keyless: Open Library + Internet Archive + Wikidata; M3.1: + Google Books)

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

### Google Books (M3.1, optional)

`gb` joins the default source set automatically when a free Google Books API key
resolves — set env `LASTCOPY_GBOOKS_KEY` or put the key in
`~/.config/lastcopy/gbooks.key` (chmod 600, **outside** the repo; never commit
it — a test scans all tracked files for key-shaped material). Without a key, `gb`
is silently omitted from the default set (one stderr notice); an explicit
`--source gb` without a key raises `KeyRequiredError`.

- Viewability mapping: `FULL_PAGES`/`ALL_PAGES` → public surrogate (GREEN
  evidence); `PARTIAL`/`SAMPLE` → partial (not an accessible surrogate);
  `NO_PAGES`/absent → no surrogate. 403 (key IP restriction) / 429 (quota) /
  empty → `unavailable`, classification stays UNKNOWN.
- The API key travels only as a request query param; it is redacted before the
  response is written to the local cache and never appears in the rate-limit
  ledger.

**IPv4 note:** if your key is IP-restricted to your host's IPv4 address but the
host egresses IPv6 by default (Google answers 403 "IP address restriction" over
IPv6), set `LASTCOPY_FORCE_IPV4=1` — the polite client then binds
`local_address=0.0.0.0` (IPv4-only egress). Off by default.

- HathiTrust / OCLC land later in M3 behind a key-signup checklist; their source
  modules are scaffolded and raise clear "key required" errors today.

## Why this exists

Reporting shows AI companies bulk-buying used books for training data and destroying
the copies — destructively scanning them, then shredding the originals. Rare,
out-of-print, foreign-language, and pre-ISBN books get pulped alongside the
millionth paperback because purchase orders are keyed to ISBNs and structurally
blind to scarcity. `lastcopy` cross-references candidate editions against digital-surrogate existence (and, later, library holdings) and emits the shortlist the
world might actually lose.

Key reporting:

- [404 Media — AI Companies Are Buying Tons of Old Books Because They're Free of "AI Slop"](https://www.404media.co/ai-companies-are-buying-tons-of-old-books-because-theyre-free-of-ai-slop/)
- [Futurism — AI Companies Are Buying Antique Books, Ingesting Their Contents, and Then Destroying Them at Incredible Scale](https://futurism.com/artificial-intelligence/ai-companies-destroying-rare-books)
- [Forbes — Are AI Companies Really Buying—And Destroying—Antique Books?](https://www.forbes.com/sites/maryroeloffs/2026/08/17/ai-companies-are-buying-and-destroying-antique-books-heres-why/)
- [Tom's Hardware — AI companies are reportedly shredding millions of books after using them to train AI models](https://www.tomshardware.com/tech-industry/artificial-intelligence/ai-companies-are-reportedly-shredding-millions-of-books-to-train-models-tech-giants-outsource-to-middlemen-to-secretly-buy-up-books-for-training-material)
- [NL Times — Rare book dealers fear tech firms destroying obscure editions to train AI models](https://nltimes.nl/2026/06/25/rare-book-dealers-fear-tech-firms-destroying-obscure-editions-train-ai-models)

## Status

- **M1 (done):** keyless OL + IA + Wikidata chain end-to-end, RED-list CSV/MD report,
  full test suite (`python3 -m pytest tests/ -q`).
- **M2 (done, stretch):** `lastcopy survey --sample N --from Y1 --to Y2` — random OL
  sample → "% no accessible digital surrogate" + Wilson 95% CI, split pre/post 1927
  (see `docs/survey-sample.md` for a live 15-book run).
- **M3.1 (done):** Google Books wired behind a free API key (viewability matrix,
  IPv4-forcing knob for IP-restricted keys, key-leak scan test); see
  `docs/RUN-M1.md` for the sample-data relabeling pass it triggered.
- **M3 (later):** HathiTrust / OCLC keys wired; confirm workflow; public dashboard.

See `SPEC.md` for the approved contract and `docs/RUN-M1.md` for a documented
end-to-end run.

## License

- Code: MIT (see `LICENSE`).
- Generated registry outputs (reports, CSVs, database dumps): CC0 (see `NOTICE`).
