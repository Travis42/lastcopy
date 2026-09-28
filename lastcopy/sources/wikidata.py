"""Wikidata SPARQL: ISBN -> work -> full-text links (Project Gutenberg / Standard Ebooks / IA)."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from ..models import Edition, HitStatus, SourceHit, SourceName, Surrogate, SurrogateAccess
from ..net import NormResponse, PoliteClient

SPARQL_URL = "https://query.wikidata.org/sparql"
PAGE_SIZE = 50  # LIMIT/OFFSET pagination
MAX_PAGES = 4

QUERY_TEMPLATE = """SELECT ?ed ?work ?fulltext WHERE {{
  VALUES ?isbn { "{isbn}" }
  ?ed wdt:P212|wdt:P957 ?isbn .
  OPTIONAL {{ ?ed wdt:P629 ?work }}
  OPTIONAL {{ ?ed wdt:P2034 ?fulltext }}
  OPTIONAL {{ ?work wdt:P2034 ?fulltext }}
}} LIMIT {limit} OFFSET {offset}"""


def provider_for_url(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "gutenberg" in host:
        return "wikidata:projectgutenberg"
    if "standardebooks" in host:
        return "wikidata:standardebooks"
    if "archive.org" in host:
        return "wikidata:internetarchive"
    return "wikidata:other"


async def check(edition: Edition, client: PoliteClient, store) -> tuple[SourceHit, list[Surrogate]]:
    if not edition.isbn13 and not edition.isbn10:
        return SourceHit(work_key=edition.work_key, source=SourceName.wd,
                         status=HitStatus.skipped,
                         evidence_json={"reason": "no ISBN (bib-stub, D1)"}), []

    isbns = [v for v in (edition.isbn13, edition.isbn10) if v]
    bindings: list[dict] = []
    endpoint_ok = False
    for isbn in isbns:
        for page in range(MAX_PAGES):
            resp = await client.get(SPARQL_URL, {
                "format": "json",
                "query": QUERY_TEMPLATE.format(isbn=isbn, limit=PAGE_SIZE,
                                               offset=page * PAGE_SIZE),
            })
            if not resp.ok:
                break  # transport/429/error: stop paging this ISBN
            endpoint_ok = True
            try:
                batch = json.loads(resp.body).get("results", {}).get("bindings", [])
            except (json.JSONDecodeError, AttributeError):
                break
            bindings.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        if bindings or endpoint_ok:
            break  # first ISBN flavor answered by the endpoint wins

    if not endpoint_ok:
        return SourceHit(work_key=edition.work_key, source=SourceName.wd,
                         status=HitStatus.unavailable,
                         evidence_json={"isbns": isbns,
                                        "reason": "SPARQL unavailable/429/empty body"}), []

    hits: list[Surrogate] = []
    urls: list[str] = []
    seen: set[str] = set()
    for b in bindings:
        ft = b.get("fulltext", {}).get("value")
        if not ft or ft in seen:
            continue
        seen.add(ft)
        hits.append(Surrogate(work_key=edition.work_key,
                              provider=provider_for_url(ft),
                              access=SurrogateAccess.public,
                              identifier=ft, url=ft))
        urls.append(ft)
    return SourceHit(work_key=edition.work_key, source=SourceName.wd,
                     status=HitStatus.ok,
                     evidence_json={"isbns": isbns, "num_bindings": len(bindings),
                                    "urls": urls[:20]}), hits
