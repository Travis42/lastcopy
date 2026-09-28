"""Google Books — SCAFFOLD, disabled without a key (SPEC D2; wiring lands in M3).

Anonymous pool is quota-exhausted (shared 429); a free API key is a human setup task.
"""

from __future__ import annotations


class KeyRequiredError(RuntimeError):
    """Raised when a gated source is invoked without its API key (M3)."""


KEY_URL = "https://console.developers.google.com/apis/api/books.googleapis.com"


async def check(*args, **kwargs):
    raise KeyRequiredError(
        "Google Books requires a free API key (anonymous pool exhausted -> 429). "
        f"Create one at {KEY_URL} and wire it in M3 (SPEC D2)."
    )
