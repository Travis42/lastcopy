# SPEC — M5.7: national-library holdings phase (Theory-directed 2026-10-02, v2.3)

## Goal
For the workset, record which legal-deposit national libraries hold a physical
copy: LOC (US), DNB (DE), BnF (FR), NDL (JA). BL deferred (catalogue down
post-cyberattack; Z39.50 registration pending Theory). Research with verified
endpoints: /root/clawd/research/2026-10-02-national-library-holdings.md.

## Data model
- New table `holdings (isbn13 TEXT, institution TEXT, record_id TEXT,
  PRIMARY KEY (isbn13, institution))` — institution ∈ {loc, dnb, bnf, ndl}.
- `enrich_status.holdings TEXT` — comma-joined institution codes (derived).
- `assign_holdings_summary(conn)`: fills enrich_status.holdings + returns counts.
- Export: `holdings` column after `custody`; acquisition-relevance note only —
  status rules UNCHANGED this phase (holdings refine priority, not CR/NT).

## Stage A — bulk downloads (new CLI `fetch-holdings-bulk --institution X`)
- dnb: full MARC21-xml set from data.dnb.de/DNB/ (37.2M records, 5 parts,
  ~12.3GB total, anonymous) + local ISBN extraction pass.
- ndl: weekly JAPAN/MARC ZIPs (small, anonymous).
- loc: MDSConnect BooksAll.2016 (43 parts ~3GB, 25M records — 2016 snapshot;
  SRU top-up covers post-2016).
- Each fetch: resumable (curl -C -), gzip/marc integrity check, files under
  data/holdings/<inst>/. CLI is download+extract only — no parsing yet.
- Parsing pass `parse-holdings-bulk --institution X`: stream MARC21-xml,
  extract ISBNs (020 $a, ISBN-10/13 normalize via isbn.py), match against
  workset (in-memory 50k set), upsert holdings rows. RAM-bounded streaming,
  progress lines, idempotent.

## Stage B — SRU top-ups (new CLI `enrich-holdings --institution X [--budget N]`)
- dnb: services.dnb.de/sru/dnb (keyless) — query isbn:"<13>"
- ndl: ndlsearch.ndl.go.jp/api/sru (keyless)
- bnf: catalogue.bnf.fr/api/SRU (keyless)
- loc: lx2.loc.gov:210/lcdb SRU over plain HTTP port 210 (TLS broken —
  accept http:// explicitly; note in code comment)
- Only for workset ISBNs with no holdings row for that institution yet
  (post-bulk residual + institutions without bulk). Rate discipline: ≤2 rps
  sustained, 429/503 backoff, IPv4-forced transport (DNB lacks IPv6),
  budget-capped, resumable (skip rows with holdings for that institution).
- Record SRU recordId as record_id.

## Tests
- MARC fixture (tiny hand-built MARC21-xml with 020 fields incl. ISBN-10 +
  hyphenated 13) → parse-holdings-bulk matches workset ISBNs, idempotent.
- SRU stubs per institution (shape-faithful: SRU namespaces, numberOfRecords)
  → holdings rows + budget/resume + http-for-loc accepted.
- assign_holdings_summary: joined codes, empty when none.
- Export: holdings column present.

## Out of scope
- BL (blocked); status-rule changes; custody changes; acquisition-ranking
  logic (next phase once holdings exist).

## Runner (I build)
fetch jobs = slurm (node-agnostic, scratch-DB for parse writes). SRU phase =
slurm array canary-first if >5k requests, else single job.
