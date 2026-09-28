"""Source clients + normalizers. Each module exposes `check(edition, client, store)`.

Shape-faithful rule: modules parse only what the real APIs return (see tests/fixtures);
error/empty/429 bodies normalize to HitStatus.unavailable / zero hits, never crash.
"""

from __future__ import annotations
