# SPEC — M8.3: UK Library Hub Z39.50 harvest — 2026-10-09

## Context (live-verified 2026-10-09 21:2x UTC from apprentice)
Jisc Library Hub Discover = UK union catalog (BL + academic + specialist, ~200+
libraries). Public Z39.50 verified: `tcp:3.250.189.53:210` (z3950.libraryhub.
jisc.ac.uk — resolve once, PIN THE IP; lab's resolver fails on the hostname).
Connection accepted (Z39.50 v3, server ess <help@jisc.ac.uk> YAZ 5.34), searches
succeed: `find 9780141439518` → 8 hits. Web/SRU faces are Cloudflare-gated; raw
Z39.50 TCP is the door. Records: server diagnostic says request XML syntax
("'try XML instead'") — preferredRecordSyntax MARCXML/XML when presenting.

## Design — lastcopy/m8_ukhub.py (runs on APPRENTICE; I/O-light, IPv4)
### 1. Z39.50 client: use `python -m PyZ3950` (pip PyZ3950) or subprocess
yaz-client batch mode — engineering choice at build; REQUIREMENT: persistent
session (open once), PQF find per ISBN (`@attr 1=7 <isbn13>` ISBN-use-attribute;
fallback plain term which also works), read hit count; retrieve records with
XML/MARCXML syntax when count>0 for institution detail (COPAC-family records
embed holdings; if fields absent or parse fails → degrade to count-only).
### 2. `harvest(workset_isbns, out_db)` — resumable (skip isbn13 with existing
row), 0.6s min-interval + jitter, one retry per transport error then record
status='err'. Store: ukhub_matches(isbn13 TEXT PRIMARY KEY, n_hits INTEGER,
institutions TEXT /* space-joined codes if parsed, else '' */, status TEXT).
Log every 2k: done/matched/errors/elapsed. Expect large match rates (English
workset vs UK libraries).
### 3. CLI `ukhub-join --workset PATH --out data/scanner/ukhub_matches.db`
### 4. Ship/import: scp matches → lab; `ukhub-import` folds into holdings
(institution='jisc-uk', detail=institutions or 'n=<count>') — same scratch
pattern as lobid-import (documented, run after k10plus/lobid write-backs).
### 5. /etc/hosts entry on lab: `3.250.189.53 z3950.libraryhub.jisc.ac.uk`
(print as manual step — I apply it).

## Tests
- PQF query build; yaz-client batch script generation (fixture)
- parser: fake XML record with 2 institution holdings → codes extracted;
  count-only degrade path; bad-line/error retry+status
- resume-skip; CLI smoke with fake workset of 3 (mock client = recorded
  transcripts in fixtures/).

## Out of scope
Jisc JSON/SRU web APIs (Cloudflare), BL direct Z39.50 application (Library Hub
already carries BL holdings — re-evaluate after first harvest), WorldCat.
