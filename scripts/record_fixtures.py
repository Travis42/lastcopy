"""Record real API fixtures for tests. Polite: UA + >=1.1s spacing per host, <=30 requests total.

Run:  python3 scripts/record_fixtures.py
Writes tests/fixtures/*.json (raw response bodies, plus a _meta.json with statuses).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from lastcopy import USER_AGENT

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)
last_ts: dict[str, float] = {}
meta: dict[str, dict] = {}
budget = 30


def get(url: str, params: dict | None = None, label: str = "", expect_status: int | None = None):
    global budget
    host = httpx.URL(url).host
    wait = 1.1 - (time.time() - last_ts.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    budget -= 1
    assert budget >= 0, "request budget exhausted"
    r = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30,
                  follow_redirects=True)
    last_ts[host] = time.time()
    name = label or url.split("/")[-1]
    (OUT / f"{name}.json").write_text(r.text, encoding="utf-8")
    meta[name] = {"url": str(r.url), "status": r.status_code}
    print(f"[{r.status_code}] {name} <- {r.request.url if hasattr(r,'request') else url} "
          f"({len(r.text)}b)")
    return r


# --- Open Library -----------------------------------------------------------
get("https://openlibrary.org/search.json",
    {"q": "isbn:9780140328721", "fields": "title,author_name,key,edition_count,oclc_numbers,first_publish_year,publisher,language,ia", "limit": 20},
    label="ol_search_hit")
get("https://openlibrary.org/search.json",
    {"q": "isbn:9780957535068", "fields": "title,author_name,key", "limit": 5},
    label="ol_search_miss")  # valid checksum, expect zero docs
get("https://openlibrary.org/search.json", {"limit": 5}, label="ol_error")  # no q -> 400
get("https://openlibrary.org/works/OL45804W/editions.json", {"page": 1},
    label="ol_editions_hit")

# --- Internet Archive ---------------------------------------------------------
get("https://archive.org/advancedsearch.php",
    {"q": "isbn:9780140328721 OR isbn:0140328726", "rows": 50, "output": "json",
     "fl[0]": "identifier", "fl[1]": "title", "fl[2]": "collection", "fl[3]": "year", "fl[4]": "creator"},
    label="ia_hit")
get("https://archive.org/advancedsearch.php",
    {"q": "isbn:9780957535068", "rows": 50, "output": "json",
     "fl[0]": "identifier", "fl[1]": "collection"},
    label="ia_miss")
get("https://archive.org/advancedsearch.php",
    {"q": "isbn:(", "rows": 5, "output": "json"},
    label="ia_error")  # malformed query

# --- Wikidata SPARQL ------------------------------------------------------------
# Exact VALUES first; Wikidata P212 is often stored hyphenated, so a normalized
# REPLACE pass finds hits the exact match misses.
Q_EXACT = ('SELECT ?ed ?work ?fulltext ?pg WHERE {{ VALUES ?isbn {{ "{isbn}" }} '
           '?ed wdt:P212|wdt:P957 ?isbn . OPTIONAL {{ ?ed wdt:P629 ?work }} '
           'OPTIONAL {{ ?ed wdt:P953 ?fulltext }} OPTIONAL {{ ?work wdt:P953 ?fulltext }} '
           'OPTIONAL {{ ?ed wdt:P2034 ?pg }} OPTIONAL {{ ?work wdt:P2034 ?pg }} }} '
           'LIMIT {limit} OFFSET {offset}')
Q_NORM = ('SELECT ?ed ?work ?fulltext ?pg WHERE {{ ?ed wdt:P212|wdt:P957 ?raw . '
          'FILTER(REPLACE(?raw, "-", "") = "{isbn}") OPTIONAL {{ ?ed wdt:P629 ?work }} '
          'OPTIONAL {{ ?ed wdt:P953 ?fulltext }} OPTIONAL {{ ?work wdt:P953 ?fulltext }} '
          'OPTIONAL {{ ?ed wdt:P2034 ?pg }} OPTIONAL {{ ?work wdt:P2034 ?pg }} }} '
          'LIMIT {limit} OFFSET {offset}')
for isbn in ["9780486417783"]:
    r = get("https://query.wikidata.org/sparql",
            {"format": "json", "query": Q_NORM.format(isbn=isbn, limit=50, offset=0)},
            label="wd_hit")
    try:
        n = len(json.loads(r.text).get("results", {}).get("bindings", []))
    except Exception:
        n = -1
    if n:
        meta["wd_hit"]["isbn"] = isbn
        break
else:
    print("WARNING: no wd_hit found")

get("https://query.wikidata.org/sparql",
    {"format": "json", "query": Q_EXACT.format(isbn="9780060935467", limit=50, offset=0)},
    label="wd_miss")  # modern bestseller, expect empty bindings
get("https://query.wikidata.org/sparql",
    {"format": "json", "query": "SELECT WHERE { ??? }"},
    label="wd_error")  # malformed SPARQL -> 400

(OUT / "_meta.json").write_text(json.dumps(meta, indent=2))
print("budget used:", 30 - budget)
