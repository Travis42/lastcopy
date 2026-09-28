"""M2 survey: random OL sample -> "% no accessible digital surrogate" + Wilson CI.

Split pre/post 1927 (copyright horizon) in the report.
"""

from __future__ import annotations

import asyncio
import json
import math

from .isbn import normalize_isbn
from .models import Edition
from .net import PoliteClient
from .store import Store

SAMPLE_URL = "https://openlibrary.org/search.json"
COPYRIGHT_HORIZON = 1927


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (k successes of n)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


async def draw_sample(client: PoliteClient, store: Store, sample_n: int,
                      year_from: int, year_to: int, max_pages: int = 10) -> list[Edition]:
    """Random OL sample within a publish-year window; keyed on first valid ISBN13."""
    editions: list[Edition] = []
    seen: set[str] = set()
    q = f"publish_year:[{year_from} TO {year_to}]"
    for page in range(max_pages):
        if len(editions) >= sample_n:
            break
        resp = await client.get(SAMPLE_URL, {
            "q": q, "fields": "isbn,title,author_name,first_publish_year",
            "sort": "random", "limit": min(sample_n * 2, 500),
            "offset": page * min(sample_n * 2, 500)})
        if not resp.ok:
            break
        try:
            docs = json.loads(resp.body).get("docs", [])
        except json.JSONDecodeError:
            break
        if not docs:
            break
        for d in docs:
            for raw in d.get("isbn") or []:
                norm = normalize_isbn(raw)
                if not norm:
                    continue
                isbn13 = norm[0]
                if isbn13 in seen:
                    continue
                seen.add(isbn13)
                editions.append(Edition(
                    work_key=isbn13, isbn13=isbn13, isbn10=norm[1],
                    title=d.get("title"),
                    author=", ".join(d.get("author_name") or []) or None,
                    year=d.get("first_publish_year"),
                    origin_note="survey"))
                store.upsert_edition(editions[-1])
                for src in ("ia", "wd"):
                    store.enqueue(isbn13, src)
                if len(editions) >= sample_n:
                    break
            if len(editions) >= sample_n:
                break
    return editions


def summarize(store: Store, editions: list[Edition]) -> dict:
    """% of sampled editions with no accessible digital surrogate + Wilson CI."""
    from .classify import classify_edition

    def no_surrogate(ed: Edition) -> bool:
        statuses = {r["source"]: r["status"] for r in store.conn.execute(
            "SELECT source, status FROM source_hits WHERE work_key=?", (ed.work_key,))}
        c = classify_edition(ed, store.surrogates_for(ed.work_key),
                             store.rarity_for(ed.work_key), statuses)
        return c.cls.value != "GREEN"

    n = len(editions)
    report: dict = {"sample": n}
    for label, eds in (("all", editions),
                       ("pre-1927", [e for e in editions if e.year and e.year < COPYRIGHT_HORIZON]),
                       ("post-1927", [e for e in editions if e.year and e.year >= COPYRIGHT_HORIZON])):
        k = sum(1 for e in eds if no_surrogate(e))
        lo, hi = wilson_ci(k, len(eds))
        report[label] = {"n": len(eds), "no_surrogate": k,
                         "pct": round(100 * k / len(eds), 1) if eds else None,
                         "wilson95": [round(lo, 3), round(hi, 3)]}
    return report


def render(report: dict) -> str:
    lines = [f"# lastcopy survey (sample={report['sample']})", ""]
    for label in ("all", "pre-1927", "post-1927"):
        s = report[label]
        if not s["n"]:
            lines.append(f"- {label}: n=0")
            continue
        lines.append(f"- {label}: n={s['n']}, no-surrogate {s['no_surrogate']} "
                     f"({s['pct']}%), Wilson 95% CI [{s['wilson95'][0]*100:.1f}%, "
                     f"{s['wilson95'][1]*100:.1f}%]")
    lines.append("")
    lines.append(f"- copyright horizon split at {COPYRIGHT_HORIZON}")
    return "\n".join(lines)
