"""LoC SRU (z3950.loc.gov) — untested stretch source per SPEC; not wired in M1."""

from __future__ import annotations


class NotImplementedYet(RuntimeError):
    pass


async def check(*args, **kwargs):
    raise NotImplementedYet(
        "LoC SRU is an untested stretch source (SPEC source inventory); not wired in M1."
    )
