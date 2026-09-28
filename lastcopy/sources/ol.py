"""Open Library: search.json (ISBN -> bib data, work key, OCLC numbers) + works/editions."""

from __future__ import annotations

import json

from ..models import Edition, HitStatus, SourceHit, SourceName, Surrogate
from ..net import NormResponse, PoliteClient

SEARCH_URL = "https://openlibrary.org/search.json"
EDITIONS_URL = "https://openlibrary.org/works/{work}/editions.json"

MAX_EDITION_PAGES = 4  # pagination cap; OL editions.json pages ~50 entries

GREEN_SURROGATE_FIELDS = "title,author_name,key,edition_count,oclc_numbers,first_publish_year,publisher,language,ia"


async def check(edition: Edition, client: PoliteClient, store) -> SourceHit:
    if not edition.isbn13 and not edition.isbn10:
        return SourceHit(work_key=edition.work_key, source=SourceName.ol,
                         status=HitStatus.skipped,
                         evidence_json={"reason": "no ISBN (bib-stub, D1)"})

    q = _or_query("isbn", [v for v in (edition.isbn13, edition.isbn10) if v])
    resp = await client.get(SEARCH_URL, {"q": q, "fields": GREEN_SURROGATE_FIELDS, "limit": 20})
    if not resp.ok:
        return _unavail(edition, resp)

    try:
        data = json.loads(resp.body)
        docs = data.get("docs", [])
    except (json.JSONDecodeError, AttributeError):
        return _unavail(edition, resp)

    if not docs:
        return SourceHit(work_key=edition.work_key, source=SourceName.ol,
                         status=HitStatus.ok, evidence_json={
                             "query": q, "numFound": data.get("numFound", 0),
                             "url": str(client.client.build_request(
                                 "GET", SEARCH_URL).url), "resolved": None})

    doc = docs[0]
    work_key = doc.get("key")  # e.g. "/works/OL45804W"
    oclc = list(doc.get("oclc_numbers") or [])[:10]
    evidence = {
        "query": q,
        "numFound": data.get("numFound", len(docs)),
        "ol_work_key": work_key,
        "title": doc.get("title"),
        "authors": doc.get("author_name") or [],
        "first_publish_year": doc.get("first_publish_year"),
        "oclc_numbers": oclc,
        "url": f"https://openlibrary.org{work_key}" if work_key else "https://openlibrary.org/search?q=" + q,
    }

    # works/{key}/editions.json — paginated; gather edition-level OCLC numbers (HT/M3 prep)
    if work_key:
        page, seen_oclc = 0, set()
        while page < MAX_EDITION_PAGES:
            eresp = await client.get(EDITIONS_URL.format(work=work_key.split("/")[-1]),
                                     {"page": page + 1})
            if not eresp.ok:
                if page == 0:
                    evidence["editions_endpoint"] = "unavailable"
                break
            try:
                entries = json.loads(eresp.body).get("entries", [])
            except json.JSONDecodeError:
                break
            if not entries:
                break
            for e in entries:
                for o in e.get("oclc_numbers") or []:
                    seen_oclc.add(str(o))
            page += 1
        evidence["edition_oclc_numbers"] = sorted(seen_oclc)[:20]

    return SourceHit(work_key=edition.work_key, source=SourceName.ol,
                     status=HitStatus.ok, evidence_json=evidence)


def _or_query(field: str, values: list[str]) -> str:
    return " OR ".join(f"{field}:{v}" for v in values)


def _unavail(edition: Edition, resp: NormResponse) -> SourceHit:
    return SourceHit(work_key=edition.work_key, source=SourceName.ol,
                     status=HitStatus.unavailable,
                     evidence_json={"reason": f"http {resp.status} / unparseable body",
                                    "status": resp.status})
