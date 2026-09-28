"""HathiTrust v3 — SCAFFOLD, disabled without a key (SPEC D2; wiring lands in M3).

Role: custodial-surrogate check (AMBER branch). Note the old brief API now rejects
isbn:/oclc: queries; v3 needs a key. HT pass two also needs an OCLC number from OL/IA.
"""

from __future__ import annotations

from .gbooks import KeyRequiredError

KEY_URL = "https://www.hathitrust.org/member-libraries/hathitrust-api-terms/"


async def check(*args, **kwargs):
    raise KeyRequiredError(
        "HathiTrust v3 API requires an applied-for key; wiring lands in M3 (SPEC D2). "
        f"See {KEY_URL}."
    )
