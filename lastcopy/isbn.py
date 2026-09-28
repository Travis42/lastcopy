"""ISBN-10 <-> ISBN-13 normalization + checksum validation (SPEC edge case #1)."""

from __future__ import annotations

import hashlib


def _clean(raw: str) -> str:
    return raw.replace("-", "").replace(" ", "").strip().upper()


def isbn10_checksum_ok(isbn10: str) -> bool:
    if len(isbn10) != 10 or not (isbn10[:9].isdigit() and (isbn10[9].isdigit() or isbn10[9] == "X")):
        return False
    total = sum(int(c) * (10 - i) for i, c in enumerate(isbn10[:9]))
    total += 10 if isbn10[9] == "X" else int(isbn10[9])
    return total % 11 == 0


def isbn13_checksum_ok(isbn13: str) -> bool:
    if len(isbn13) != 13 or not isbn13.isdigit():
        return False
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn13[:12]))
    return (10 - total % 10) % 10 == int(isbn13[12])


def isbn10_to_13(isbn10: str) -> str:
    core = "978" + isbn10[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def isbn13_to_10(isbn13: str) -> str:
    if not isbn13.startswith("978"):
        return ""  # no ISBN-10 equivalent exists for 979- space
    core = isbn13[3:12]
    total = sum(int(c) * (10 - i) for i, c in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


def normalize_isbn(raw: str) -> tuple[str, str] | None:
    """Return (isbn13, isbn10) for a valid ISBN of either flavor, else None."""
    if not raw:
        return None
    v = _clean(raw)
    if len(v) == 13 and v.isdigit():
        if not isbn13_checksum_ok(v):
            return None
        ten = isbn13_to_10(v)
        return (v, ten or None)
    if len(v) == 10:
        if not isbn10_checksum_ok(v):
            return None
        return (isbn10_to_13(v), v)
    return None


def norm_bib(title: str, author: str, year: str | int | None) -> str:
    """Normalize title|author|year for the sha1 bib work_key (pre-ISBN rows)."""
    return "|".join(
        [
            " ".join((title or "").lower().split()),
            " ".join((author or "").lower().split()),
            str(year or "").strip(),
        ]
    )


def bib_work_key(title: str, author: str, year: str | int | None) -> str:
    return "bib-" + hashlib.sha1(norm_bib(title, author, year).encode()).hexdigest()
