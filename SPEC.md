# SPEC — `lastcopy`: Last-Copy Registry prototype (v0.1)

**Date:** 2026-09-28 · **Status:** APPROVED 2026-09-28 (Theory) — D1=stub, D2=keyless M1 start, D3=public repo day one
**Context:** AI bulk-buyers purchase books ISBN-keyed, blind to scarcity, then shred.
No public artifact answers: *"which editions have no accessible digital surrogate and
few surviving copies?"* This is that artifact.

---

## Architecture gate (per AGENTS 2026-09-19)

- **Workload:** network-latency-bound API mashup; thousands → low millions of records;
  zero numeric intensity; moderate branching (per-source normalizers); streaming-friendly.
- **Verdict: CPU, async I/O** — `asyncio` + `httpx`, bounded per-host concurrency
  (default 1 req/s/host, jittered). GPU irrelevant; bottleneck is remote-API latency
  and politeness quotas. Scale-out shape = more worker processes against the shared
  SQLite queue, not bigger cores. CPU reference = the pipeline itself (it is trivially CPU).

## Problem

Bulk AI purchase orders are keyed to ISBNs and structurally blind to scarcity.
Rare / out-of-print / foreign-language / pre-ISBN books get pulped alongside the
millionth paperback. The registry cross-references candidate editions against
digital-surrogate existence and (for the shortlist) library holdings, and emits a
**RED list**: editions where the world could actually lose the last accessible copy.

## Users

1. **Booksellers** — paste a bulk-lot ISBN list → RED flags to pull before shipping.
2. **Journalists** — survey stat: "% of sampled editions have no accessible digital
   surrogate," with confidence interval. The press number.
3. **Local pull (Theory)** — triage a shelf of Portuguese/Azorean books, many pre-ISBN
   (bib mode).

## Non-goals (v1)

- No web dashboard (CLI + CSV/SQLite + Markdown report; dashboard is M3+).
- No scraping of gated sources (OCLC web pages, BookFinder, AbeBooks) — official API
  keys or manual shortlist verification only. ToS-clean by construction.
- No automatic purchasing, no outreach automation.

## Classification matrix

For each edition (roll-up to work level for reporting):

| Condition | Class |
|---|---|
| Public surrogate exists (IA public scan, IA lending scan, Google Books FULL, Project Gutenberg, Standard Ebooks) | **GREEN** |
| Custodial-only surrogate (HathiTrust search-only) | **AMBER** — survives in custody, no public access |
| No surrogate + OCLC holdings ≤ 5 | **RED** |
| No surrogate + holdings unknown / for-sale ≤ 2 | **RED-UNVERIFIED** (confirmation queue) |
| No data yet / source down | **UNKNOWN** (never silently GREEN) |

Every classification carries a machine-readable rationale citing source + evidence URL.

## Source inventory (live-probed 2026-09-28)

| Source | Endpoint | Auth | Role | Probe result |
|---|---|---|---|---|
| Open Library search | `openlibrary.org/search.json` (+works/editions) | none | bib data, edition links | ✅ works |
| Internet Archive | `archive.org/advancedsearch.php` (`isbn:` / `oclc:`) | none | scan existence + lending status | ✅ (standard public API) |
| Wikidata SPARQL | `query.wikidata.org` | none | ISBN→work→fulltext links (PG/IA) | ✅ stable public |
| Google Books | `googleapis.com/books/v1/volumes` | free API key | viewability (NO_PAGES/PARTIAL/FULL) | ⚠️ anonymous pool exhausted (shared quota 429) → key required |
| HathiTrust v3 | `catalog.hathitrust.org/api/v3/...` | key required | custodial-surrogate check | ⚠️ old brief API now rejects `isbn/`/`oclc/` queries — deprecated |
| WorldCat Search API v2 | OCLC dev portal | WSKey + dev account | holdings count — **confirmation layer, shortlist only** | gated; setup task |
| LoC SRU | `z3950.loc.gov` | none | LoC holdings (weak for PT/Azores; stretch) | untested |

**Human setup tasks (not OpenCode):** create Google Books key; apply for HathiTrust
key; register OCLC WSKey. M1 must work without any of them.

## Data model (SQLite)

```
editions(work_key PK, isbn13, isbn10, title, author, year, publisher,
         imprint_place, language, origin_note)
  -- work_key = isbn13 when present, else sha1(norm(title|author|year))
source_hits(work_key, source, status, checked_at, evidence_json)
surrogates(work_key, provider, access ENUM(public|lending|custodial|partial),
           identifier, url)
rarity(work_key, oclc_holdings, for_sale_count, method, checked_at)
classification(work_key, cls, rationale, computed_at)
runs(...); ratelimit_ledger(...); cache(TTL 30d, keyed on url+params)
```

## CLI

```
lastcopy ingest  --csv lots.csv [--bib-mode]          # candidates in (ISBN or bib rows)
lastcopy enrich  [--workers 4] [--source ol,ia,wd,gb,ht,loc]
lastcopy confirm --csv shortlist.csv                  # manual OCLC/BookFinder annotations in
lastcopy classify                                     # apply matrix
lastcopy report  [--md out.md] [--csv out.csv]        # RED list + counts + roll-up
lastcopy survey   --sample 5000 --from 1900 --to 1980 # random OL sample → % no-surrogate + Wilson CI
```

Politeness: 1 rps/host default, jitter; UA `lastcopy/0.1 (+contact email)`;
everything cached; fully resumable (crash-safe queue in SQLite).

## Modules

```
lastcopy/
  models.py               # pydantic models shared everywhere
  sources/ol.py ia.py wikidata.py gbooks.py hathi.py loc.py   # thin clients + normalizers
  classify.py store.py cli.py survey.py
tests/
  fixtures/*.json         # recorded real payloads incl. empty / error / 429 bodies
  test_sources_*.py test_classify.py test_e2e.py test_politeness.py
```

## Edge cases

- ISBN10↔13 normalization + checksum validation; invalid/absent ISBNs rejected to bib-mode.
- **Pre-ISBN works** (pre-~1970): keyed on normalized title|author|year; OL title-search
  resolution; ambiguity → UNKNOWN, never guess.
- Multi-edition collapse: classify per edition, report per work.
- HT lookup needs OCLC number → obtain from OL/IA first pass, then HT pass two.
- Source down / quota exhausted → status=unavailable, classification stays UNKNOWN.
- Survey slices pre-1927 vs post-1927 (copyright horizon) in the report.

## Tests (mandatory, per AGENTS)

- Unit: normalizers against recorded fixtures, shape-faithful (match real response
  types, incl. error/429/empty bodies — stub the transport boundary).
- Matrix: every classification cell exercised, including negative paths (absent
  evidence must never produce GREEN).
- E2E: fixture store → enrich(mock transport) → classify → report golden file.
- Politeness/cache: rate ledger enforcement, cache-hit skip, resume after crash.
- One regression test per incident, named for it.

## Locked decisions (Theory, 2026-09-28)

- **D1 — bib mode:** STUB in v1. Pre-ISBN rows are ingested with a generated
  work_key, flagged `bib-stub`, and queued for future fuzzy resolution. No OL
  title-search resolution in v1 (keep ambiguity visible, never guessed).
- **D2 — start:** M1 keyless (Open Library + Internet Archive + Wikidata only).
  Google Books / HathiTrust / OCLC land in M3 behind a key-signup checklist.
  GB/HT source modules are scaffolded but disabled without keys.
- **D3 — publishing:** Public repo from day one. License: **MIT for code, CC0 for
  generated registry outputs/data.** GitHub push happens after human review of M1,
  not from the build agent. (Owner/name: `lastcopy` under Theory's account, unless
  overridden.)

## M3.1 — Google Books wiring (key delivered 2026-09-29, Theory)

**Key handling (hard rules):** key lives in env `LASTCOPY_GBOOKS_KEY` or file
`~/.config/lastcopy/gbooks.key` (chmod 600, already in place on apprentice).
NEVER hardcode it; never commit it; a test must scan all tracked files for the
`AIza` key prefix and fail if found. Add `.env` to .gitignore.

**sources/gbooks.py:** query `googleapis.com/books/v1/volumes?q=isbn:{isbn13}&key=…`
through PoliteClient (host `googleapis.com`, same 1 rps + jitter politeness).
Map `accessInfo.viewability`: `FULL_PAGES` → surrogate access `public` (GREEN
evidence); `PARTIAL`/`SAMPLE` → `partial`; `NO_PAGES`/absent → no surrogate.
Record source-side title/author/year in evidence (see sample-verification below).
403 (IP restriction) / 429 (quota) / empty → status `unavailable`, classification
stays UNKNOWN per matrix. Multi-item responses: first item decides viewability,
all identifiers recorded.

**cli enrich:** `--source` accepts `gb`; default source set = `ol,ia,wd` + `gb`
when a key resolves (env or file), without `gb` otherwise (single stderr notice,
not an error).

**Sample-data verification pass (bug found 2026-09-29):** samples/lots.csv was
agent-authored from memory and contains at least one mislabel — `9780140283334`
is *Lord of the Flies* (GB: 1999 Penguin), not *The Brothers Karamazov* as our
label claimed. During M3.1: cross-check every ISBN row of samples/lots.csv
against OL + GB titles; fix mislabels; replace the Karamazov slot with a real
in-copyright Karamazov edition (e.g. 9780374528379 Pevear/Volokhonsky) so the
sample still exercises a modern in-copyright RED-UNVERIFIED case; document the
relabeling in docs/RUN-M1.md (the prior "RED: Karamazov" line was really LotF).
Longer-term note (v2 fodder, do not build now): per-source title disagreement is
itself a signal — log it when seen.

**Tests:** recorded fixtures via `curl -4` (IPv4 is allowlisted; IPv6 restriction
fix pending on Theory's side) — strip the key param from fixture URLs before
saving; viewability-mapping matrix tests; no-key behavior (`--source gb` without
key raises KeyRequiredError; default set silently omits gb); key-leak scan test;
unavailable-normalization test (403/429 bodies).

**Acceptance:** pytest green incl. new tests; live `--source gb` enrich against
samples/lots.csv executed with `curl -4`-style IPv4 forcing or after the IPv6
allowlist entry lands (defer that one live check if needed — say so in the report);
report shows GB evidence rows; README updated.

## M3.2 — Survey: rarity slices + gb + citable rows (Theory steer, 2026-09-29)

Steer: the end focus is rare titles, not beloved classics. The survey must measure
rare-material risk, not just universe prevalence.

1. **draw_sample:** fetch `edition_count` and `language` in OL fields; persist on the
   editions row (new nullable columns `edition_count INTEGER`, `language TEXT` —
   migrate with a pragma column-check + ALTER TABLE, safe on existing DBs).
2. **CLI:** `lastcopy survey --sample N --from Y1 --to Y2 [--source ia,gb] [--filter F]
   [--rows out.csv] [--md out.md]`. Survey default sources = `ia,gb` (wd optional:
   ~3× wall cost for marginal signal on this stat — registry mode keeps wd).
   `--filter F` appends to the OL query (e.g. `language:por` → Azores/PT follow-up
   survey without code changes).
3. **Slices in summarize + render, per-cell Wilson 95%:** edition_count buckets
   1 / 2–3 / ≥4; language `eng` vs non-eng; era pre-1927 / 1927–1969 / 1970+;
   plus the `all` headline. Cells with n<30 get a `thin-sample` flag in output.
   **Decided vs unknown:** rows classifying UNKNOWN (source down) are reported
   separately; headline pct is over decided rows only, with n stated.
4. **`--rows` CSV (the citable dataset, CC0 per NOTICE):** work_key, isbn13, title,
   author, year, edition_count, language, sources_checked, surrogate providers+access,
   class, rule.
5. **Tests:** draw fixture with new fields incl. missing edition_count/language;
   slice bucket boundaries (1 vs 2 vs 4; era edges 1926/1927/1969/1970); unknown
   split; rows-CSV golden; thin-cell flag. Mock transport throughout.
6. **Acceptance:** pytest green; one live verification run (`--sample 30 --from 1890
   --to 1999 --rows /tmp/…`) documented in the report. The 5k production run is
   launched by Apprentice after review — not from the build.

## M1 acceptance criteria

1. `pip install -e .` works; `lastcopy --help` shows ingest/enrich/classify/report.
2. End-to-end on a shipped sample CSV (~20 real ISBNs: one famous public-domain
   work with a known IA scan → expect GREEN; one modern in-print bestseller →
   expect RED-UNVERIFIED or better per evidence; several obscure/pre-1970 rows,
   ≥3 pre-ISBN bib-stub rows → expect stub queue entries). Every classification
   carries evidence URLs.
3. `python3 -m pytest tests/ -q` green, including matrix tests, politeness/cache
   tests, and e2e with mock transport.
4. README with quickstart + a short "why this exists" section linking the 404
   Media / Futurism reporting.
5. Git repo initialized locally, conventional commits. **No remote push, no
   GitHub repo creation from the build agent.**

## Milestones (M2 = stretch, only if M1 is fully green)

- **M1 (keyless, REQUIRED):** OL + IA + Wikidata chain end-to-end: ingest → enrich →
  classify → RED-list CSV/MD report. No keys required. Proves the signal.
- **M2 (stretch):** survey mode — `lastcopy survey --sample N --from Y1 --to Y2`
  → "% no accessible digital surrogate" + Wilson CI, split pre/post 1927.
- **M3 (later):** Google/HT/OCLC keys wired; confirm workflow; public dashboard.
