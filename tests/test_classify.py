"""Classification matrix: every cell, verbatim rules, incl. injected-holdings RED (M3 prep)."""

import pytest

from lastcopy.classify import classify_edition
from lastcopy.models import Cls, Edition, Rarity, Surrogate, SurrogateAccess

ED = Edition(work_key="9780140328721", isbn13="9780140328721", origin_note="csv")
OK = {"ol": "ok", "ia": "ok", "wd": "ok"}


def _s(access=SurrogateAccess.public, provider="ia", url="https://archive.org/details/x"):
    return Surrogate(work_key=ED.work_key, provider=provider, access=access,
                     identifier="x", url=url)


def test_green_public_surrogate():
    c = classify_edition(ED, [_s(SurrogateAccess.public)], None, OK)
    assert c.cls is Cls.GREEN
    assert c.rationale["rule"] == "public-surrogate"
    assert "https://archive.org/details/x" in c.rationale["evidence_urls"]


def test_green_lending_scan_is_public_surrogate():
    # matrix row 1: "IA public scan, IA lending scan" both count as GREEN
    c = classify_edition(ED, [_s(SurrogateAccess.lending)], None, OK)
    assert c.cls is Cls.GREEN


def test_green_project_gutenberg():
    c = classify_edition(ED, [_s(SurrogateAccess.public, provider="wikidata:projectgutenberg",
                                 url="https://www.gutenberg.org/ebooks/215")], None, OK)
    assert c.cls is Cls.GREEN
    assert c.rationale["providers"] == ["wikidata:projectgutenberg"]


def test_amber_custodial_only():
    c = classify_edition(ED, [_s(SurrogateAccess.custodial, provider="hathitrust")], None, OK)
    assert c.cls is Cls.AMBER
    assert c.rationale["rule"] == "custodial-only-surrogate"


def test_red_injected_holdings_le_5():
    # M1 has no holdings counts; branch kept + tested against injected values for M3
    c = classify_edition(ED, [], Rarity(work_key=ED.work_key, oclc_holdings=5), OK)
    assert c.cls is Cls.RED
    assert c.rationale["rule"] == "no-surrogate+holdings<=5"
    c = classify_edition(ED, [], Rarity(work_key=ED.work_key, oclc_holdings=1), OK)
    assert c.cls is Cls.RED


def test_red_unverified_holdings_unknown():
    # M1 keyless default: no surrogate, no holdings -> confirmation queue
    c = classify_edition(ED, [], None, OK)
    assert c.cls is Cls.RED_UNVERIFIED
    assert c.rationale["rule"] == "no-surrogate+holdings-unknown"


def test_red_unverified_for_sale_le_2():
    c = classify_edition(ED, [], Rarity(work_key=ED.work_key, oclc_holdings=50,
                                        for_sale_count=2), OK)
    assert c.cls is Cls.RED_UNVERIFIED
    assert c.rationale["rule"] == "no-surrogate+for-sale<=2"


def test_unknown_source_down():
    c = classify_edition(ED, [], None, {"ol": "ok", "ia": "unavailable", "wd": "ok"})
    assert c.cls is Cls.UNKNOWN
    assert c.rationale["rule"] == "source-unavailable"
    assert c.rationale["unavailable_sources"] == ["ia"]


def test_unknown_no_data_yet():
    c = classify_edition(ED, [], None, {})
    assert c.cls is Cls.UNKNOWN
    assert c.rationale["rule"] == "no-data-yet"


def test_unknown_bib_stub():
    ed = Edition(work_key="bib-abc", title="T", author="A", origin_note="bib-stub:no-isbn")
    c = classify_edition(ed, [], None, {"ol": "skipped", "ia": "skipped", "wd": "skipped"})
    assert c.cls is Cls.UNKNOWN
    assert c.rationale["rule"] == "bib-stub"


def test_gap_no_surrogate_holdings_gt_5_is_unknown():
    # documented ambiguity: matrix has no cell for no-surrogate + holdings > 5
    c = classify_edition(ED, [], Rarity(work_key=ED.work_key, oclc_holdings=120), OK)
    assert c.cls is Cls.UNKNOWN
    assert c.rationale["rule"] == "no-surrogate+holdings>5"


def test_absent_evidence_never_green():
    # negative path: nothing found anywhere must not produce GREEN
    for statuses in ({}, {"ol": "ok"}, {"ol": "ok", "ia": "unavailable"}):
        c = classify_edition(ED, [], None, statuses)
        assert c.cls is not Cls.GREEN


def test_surrogate_wins_over_unavailable_source():
    # IA found a public scan while OL is down: matrix row 1 outranks row 5
    c = classify_edition(ED, [_s(SurrogateAccess.public)], None,
                         {"ol": "unavailable", "ia": "ok", "wd": "unavailable"})
    assert c.cls is Cls.GREEN
