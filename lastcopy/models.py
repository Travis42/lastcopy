"""Pydantic models shared across the pipeline (mapped onto the SQLite schema in SPEC.md)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceName(str, Enum):
    ol = "ol"
    ia = "ia"
    wd = "wd"
    gb = "gb"
    ht = "ht"
    loc = "loc"
    oclc = "oclc"


class HitStatus(str, Enum):
    """Normalized per-source outcome for one work_key."""

    ok = "ok"                # source answered; may still contain zero hits
    unavailable = "unavailable"  # network error, non-200, 429/quota, unparseable body
    skipped = "skipped"      # e.g. bib-stub rows are never resolved in M1 (D1)


class SurrogateAccess(str, Enum):
    public = "public"
    lending = "lending"
    custodial = "custodial"
    partial = "partial"


class Cls(str, Enum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"
    RED_UNVERIFIED = "RED-UNVERIFIED"
    UNKNOWN = "UNKNOWN"


class Edition(BaseModel):
    work_key: str  # isbn13 when present, else sha1(norm(title|author|year))
    isbn13: str | None = None
    isbn10: str | None = None
    title: str | None = None
    author: str | None = None
    year: int | None = None
    publisher: str | None = None
    imprint_place: str | None = None
    language: str | None = None
    edition_count: int | None = None  # OL work edition_count (survey rarity slice, M3.2)
    origin_note: str = ""  # 'csv', 'csv:invalid-isbn->bib-stub', 'bib-mode', ...


class SourceHit(BaseModel):
    work_key: str
    source: SourceName
    status: HitStatus
    checked_at: datetime = Field(default_factory=utcnow)
    evidence_json: dict = Field(default_factory=dict)


class Surrogate(BaseModel):
    work_key: str
    provider: str  # 'ia', 'wikidata:projectgutenberg', 'wikidata:standardebooks', ...
    access: SurrogateAccess
    identifier: str
    url: str


class Rarity(BaseModel):
    work_key: str
    oclc_holdings: int | None = None  # None = unknown (M1 keyless: always None)
    for_sale_count: int | None = None
    method: str = "none"
    checked_at: datetime = Field(default_factory=utcnow)


class Classification(BaseModel):
    work_key: str
    cls: Cls
    rationale: dict  # machine-readable: rule, surrogate providers, holdings, sources considered
    computed_at: datetime = Field(default_factory=utcnow)

    @property
    def evidence_urls(self) -> list[str]:
        return list(self.rationale.get("evidence_urls", []))
