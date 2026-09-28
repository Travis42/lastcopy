"""Classification matrix (SPEC, verbatim) applied per edition; roll-up happens at report time.

| Public surrogate exists (IA public scan, IA lending scan, Google Books FULL,
| Project Gutenberg, Standard Ebooks)                                    -> GREEN
| Custodial-only surrogate (HathiTrust search-only)                      -> AMBER
| No surrogate + OCLC holdings <= 5                                      -> RED
| No surrogate + holdings unknown / for-sale <= 2                        -> RED-UNVERIFIED
| No data yet / source down                                              -> UNKNOWN (never silently GREEN)

M1 keyless has no holdings counts (OCLC lands in M3), so no-surrogate => RED-UNVERIFIED.
The RED branch is implemented and unit-tested against injected holdings values for M3.
"""

from __future__ import annotations

from .models import (
    Classification,
    Cls,
    Edition,
    HitStatus,
    Rarity,
    Surrogate,
    SurrogateAccess,
    utcnow,
)

PUBLIC_ACCESS = {SurrogateAccess.public, SurrogateAccess.lending}  # both GREEN per matrix row 1
KEYLESS_SOURCES = {"ol", "ia", "wd"}


def classify_edition(
    edition: Edition,
    surrogates: list[Surrogate],
    rarity: Rarity | None = None,
    hit_statuses: dict[str, str] | None = None,  # {"ol": "ok", "ia": "unavailable", ...}
) -> Classification:
    statuses = hit_statuses or {}
    urls: list[str] = []

    public = [s for s in surrogates if s.access in PUBLIC_ACCESS]
    custodial = [s for s in surrogates if s.access == SurrogateAccess.custodial]

    # Row 1: GREEN — public/lending surrogate (IA scan, PG, SE; GB FULL is M3)
    if public:
        urls = [s.url for s in public]
        return Classification(
            work_key=edition.work_key, cls=Cls.GREEN,
            rationale=_r("public-surrogate",
                         providers=[s.provider for s in public],
                         evidence_urls=urls),
        )

    # Row 2: AMBER — custodial-only surrogate
    if custodial:
        urls = [s.url for s in custodial]
        return Classification(
            work_key=edition.work_key, cls=Cls.AMBER,
            rationale=_r("custodial-only-surrogate",
                         providers=[s.provider for s in custodial],
                         evidence_urls=urls),
        )

    # Row 5a: source down -> UNKNOWN (never silently GREEN, never confidently RED)
    unavailable = sorted(src for src, st in statuses.items() if st == HitStatus.unavailable.value)
    if unavailable:
        return Classification(
            work_key=edition.work_key, cls=Cls.UNKNOWN,
            rationale=_r("source-unavailable",
                         unavailable_sources=unavailable,
                         note="classification stays UNKNOWN when a source is down"),
        )

    # Row 5b: no data yet (bib-stub rows under D1 — queued, not resolved in M1)
    if not statuses or all(st == HitStatus.skipped.value for st in statuses.values()):
        rule = "bib-stub" if (edition.origin_note or "").startswith("bib") else "no-data-yet"
        return Classification(
            work_key=edition.work_key, cls=Cls.UNKNOWN,
            rationale=_r(rule, note="D1: bib-stubs are queued for future fuzzy resolution"),
        )

    # Rows 3/4: no surrogate found and sources answered OK.
    holdings = rarity.oclc_holdings if rarity else None
    for_sale = rarity.for_sale_count if rarity else None
    if holdings is not None and holdings <= 5:
        return Classification(
            work_key=edition.work_key, cls=Cls.RED,
            rationale=_r("no-surrogate+holdings<=5", oclc_holdings=holdings,
                         evidence_urls=[]),
        )
    if for_sale is not None and for_sale <= 2:
        return Classification(
            work_key=edition.work_key, cls=Cls.RED_UNVERIFIED,
            rationale=_r("no-surrogate+for-sale<=2", for_sale_count=for_sale,
                         evidence_urls=[]),
        )
    # M1 keyless: holdings always unknown -> RED-UNVERIFIED (confirmation queue)
    if holdings is None:
        return Classification(
            work_key=edition.work_key, cls=Cls.RED_UNVERIFIED,
            rationale=_r("no-surrogate+holdings-unknown",
                         oclc_holdings=None,
                         sources_answered=sorted(statuses),
                         evidence_urls=[]),
        )
    # Not covered by the matrix: no surrogate + holdings > 5. Minimal choice: UNKNOWN
    # with explicit rationale (documented ambiguity; no new class invented).
    return Classification(
        work_key=edition.work_key, cls=Cls.UNKNOWN,
        rationale=_r("no-surrogate+holdings>5", oclc_holdings=holdings,
                     note="no matrix cell: many copies survive, not last-copy at risk"),
    )


def _r(rule: str, **extra) -> dict:
    r = {"rule": rule, "computed_at": utcnow().isoformat()}
    r.update(extra)
    return r
