"""Google Books: volumes?q=isbn:{isbn13}&key=... -> viewability surrogates (M3.1).

Key handling (SPEC M3.1 hard rules): the key lives in env LASTCOPY_GBOOKS_KEY or
in ~/.config/lastcopy/gbooks.key (chmod 600, outside the repo). It is NEVER
hardcoded or committed; a test scans all tracked files for Google-key-shaped
material and fails if found.

Viewability map (accessInfo.viewability of the FIRST item decides):
  FULL_PAGES / ALL_PAGES -> SurrogateAccess.public  (GREEN evidence, matrix row 1;
      ALL_PAGES is what Google's scanned public-domain books actually return —
      full view, no ISBN, seen live 2026-09-29; FULL_PAGES is the spec's spelling)
  PARTIAL / SAMPLE -> SurrogateAccess.partial (not a public surrogate)
  NO_PAGES / absent -> no surrogate
403 (key IP restriction) / 429 (quota) / empty -> status unavailable; the
classification stays UNKNOWN per matrix. All identifiers in a multi-item
response are recorded in evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..models import (
    Edition,
    HitStatus,
    SourceHit,
    SourceName,
    Surrogate,
    SurrogateAccess,
)
from ..net import NormResponse, PoliteClient

KEY_ENV = "LASTCOPY_GBOOKS_KEY"
KEY_FILE = Path.home() / ".config" / "lastcopy" / "gbooks.key"

VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"
KEY_URL = "https://console.developers.google.com/apis/api/books.googleapis.com"

VIEWABILITY_ACCESS = {
    "FULL_PAGES": SurrogateAccess.public,
    "ALL_PAGES": SurrogateAccess.public,
    "PARTIAL": SurrogateAccess.partial,
    "SAMPLE": SurrogateAccess.partial,
}


class KeyRequiredError(RuntimeError):
    """Raised when a gated source is invoked without its API key."""


def resolve_key() -> str | None:
    """Env first, then the key file. Never logs or embeds the key value."""
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def no_key_error() -> KeyRequiredError:
    return KeyRequiredError(
        "Google Books requires a free API key (anonymous pool exhausted -> 429). "
        f"Set {KEY_ENV} or put it in {KEY_FILE}; create one at {KEY_URL}."
    )


async def check(
    edition: Edition, client: PoliteClient, store
) -> tuple[SourceHit, list[Surrogate]]:
    key = resolve_key()
    if not key:
        raise no_key_error()

    isbns = [v for v in (edition.isbn13, edition.isbn10) if v]
    if not isbns:
        return SourceHit(work_key=edition.work_key, source=SourceName.gb,
                         status=HitStatus.skipped,
                         evidence_json={"reason": "no ISBN (bib-stub, D1)"}), []

    q = f"isbn:{isbns[0]}"
    resp = await client.get(VOLUMES_URL, {"q": q, "key": key})
    if not resp.ok:
        # 403 = key IP restriction, 429 = quota, 0 = transport error, empty body
        return _unavail(edition, resp), []

    try:
        data = json.loads(resp.body)
    except json.JSONDecodeError:
        return _unavail(edition, resp), []

    items = data.get("items") or []
    evidence: dict = {
        "query": q,
        # evidence URL is the keyless canonical form; the real request adds &key=
        "url": f"{VOLUMES_URL}?q={q}",
        "totalItems": data.get("totalItems", len(items)),
        "identifiers": [it.get("id") for it in items if it.get("id")],
    }
    if not items:
        return SourceHit(work_key=edition.work_key, source=SourceName.gb,
                         status=HitStatus.ok, evidence_json=evidence), []

    first = items[0].get("volumeInfo", {})
    viewability = items[0].get("accessInfo", {}).get("viewability")
    access = VIEWABILITY_ACCESS.get(viewability)
    evidence["viewability"] = viewability
    evidence["title"] = first.get("title")
    evidence["authors"] = first.get("authors") or []
    evidence["published_date"] = first.get("publishedDate")

    surrogates: list[Surrogate] = []
    if access is not None:
        vol_id = items[0].get("id") or isbns[0]
        surrogates.append(Surrogate(
            work_key=edition.work_key, provider="gb", access=access,
            identifier=vol_id,
            url=first.get("previewLink") or f"https://books.google.com/books?id={vol_id}"))
    return SourceHit(work_key=edition.work_key, source=SourceName.gb,
                     status=HitStatus.ok, evidence_json=evidence), surrogates


def _unavail(edition: Edition, resp: NormResponse) -> SourceHit:
    return SourceHit(work_key=edition.work_key, source=SourceName.gb,
                     status=HitStatus.unavailable,
                     evidence_json={"reason": f"http {resp.status} / unparseable body "
                                              "(403=IP restriction, 429=quota)",
                                    "status": resp.status})
