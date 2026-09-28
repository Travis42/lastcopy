"""Internet Archive: advancedsearch.php isbn:/oclc: queries -> public/lending scans."""

from __future__ import annotations

import json

from ..models import Edition, HitStatus, SourceHit, SourceName, Surrogate, SurrogateAccess
from ..net import NormResponse, PoliteClient

SEARCH_URL = "https://archive.org/advancedsearch.php"
LENDING_COLLECTIONS = {"inlibrary", "printdisabled"}
DETAIL_BASE = "https://archive.org/details/"

FL = ["identifier", "title", "collection", "year", "creator"]
ROWS = 50


async def check(edition: Edition, client: PoliteClient, store) -> tuple[SourceHit, list[Surrogate]]:
    queries: list[tuple[str, str]] = []  # (query, note)
    isbns = [v for v in (edition.isbn13, edition.isbn10) if v]
    if isbns:
        queries.append((" OR ".join(f"isbn:{v}" for v in isbns), "isbn"))
    oclc = await _ol_oclc(store, edition.work_key)
    if oclc:
        queries.append((f"oclc:{oclc}", "oclc"))

    if not queries:
        return SourceHit(work_key=edition.work_key, source=SourceName.ia,
                         status=HitStatus.skipped,
                         evidence_json={"reason": "no ISBN and no OCLC (bib-stub, D1)"}), []

    hits, evidence_urls = [], []
    ok_seen, unavail_seen = False, False
    for q, note in queries:
        resp = await client.get(SEARCH_URL, {"q": q, "rows": ROWS, "output": "json",
                                             **{f"fl[{i}]": f for i, f in enumerate(FL)}})
        if not resp.ok:
            unavail_seen = True
            continue
        try:
            docs = json.loads(resp.body).get("response", {}).get("docs", [])
        except (json.JSONDecodeError, AttributeError):
            unavail_seen = True
            continue
        ok_seen = True
        for d in docs:
            ident = d.get("identifier")
            if not ident:
                continue
            collections = {str(c).lower() for c in (d.get("collection") or [])}
            access = (SurrogateAccess.lending
                      if collections & LENDING_COLLECTIONS else SurrogateAccess.public)
            hits.append(Surrogate(work_key=edition.work_key, provider="ia",
                                  access=access, identifier=ident,
                                  url=DETAIL_BASE + ident))
            evidence_urls.append(DETAIL_BASE + ident)

    if not ok_seen and unavail_seen:
        return SourceHit(work_key=edition.work_key, source=SourceName.ia,
                         status=HitStatus.unavailable,
                         evidence_json={"reason": "all IA queries failed"}), []

    return SourceHit(work_key=edition.work_key, source=SourceName.ia,
                     status=HitStatus.ok,
                     evidence_json={"queries": [q for q, _ in queries],
                                    "num_hits": len(hits),
                                    "urls": evidence_urls[:20]}), hits


async def _ol_oclc(store, work_key: str) -> str | None:
    """IA oclc: fallback needs an OCLC number obtained from the OL pass first."""
    import sqlite3
    try:
        row = store.conn.execute(
            "SELECT evidence_json FROM source_hits WHERE work_key=? AND source='ol'",
            (work_key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        ev = json.loads(row["evidence_json"])
    except json.JSONDecodeError:
        return None
    for k in ("oclc_numbers", "edition_oclc_numbers"):
        vals = ev.get(k) or []
        if vals:
            return str(vals[0])
    return None
