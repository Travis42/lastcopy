"""M3.2 survey: random OL sample -> "% no accessible digital surrogate" + Wilson CI.

Rarity slices per-cell Wilson 95%: edition_count 1 / 2-3 / >=4; language eng vs
non-eng; era pre-1927 / 1927-1969 / 1970+; plus the `all` headline. UNKNOWN rows
(source down) are reported separately; the headline pct is over decided rows
only, with n stated. Cells with n<30 carry a thin-sample flag.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from .classify import classify_edition
from .isbn import normalize_isbn
from .models import Classification, Edition
from .net import PoliteClient
from .store import Store

SAMPLE_URL = "https://openlibrary.org/search.json"
THIN_SAMPLE_N = 30

SLICE_ORDER = [
    "all",
    "editions:1", "editions:2-3", "editions:4+",
    "lang:eng", "lang:non-eng",
    "era:pre-1927", "era:1927-1969", "era:1970+",
]

ROWS_HEADER = ["work_key", "isbn13", "title", "author", "year", "edition_count",
               "language", "sources_checked", "surrogates", "class", "rule"]


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (k successes of n)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


def edition_slice(ec: int | None) -> str | None:
    if ec is None:
        return None
    if ec <= 1:
        return "editions:1"
    if ec <= 3:
        return "editions:2-3"
    return "editions:4+"


def era_slice(year: int | None) -> str | None:
    if year is None:
        return None
    if year < 1927:
        return "era:pre-1927"
    if year <= 1969:
        return "era:1927-1969"
    return "era:1970+"


def language_slice(language: str | None) -> str | None:
    if not language:
        return None
    return "lang:eng" if language.strip().lower().startswith("en") else "lang:non-eng"


def slices_for(ed: Edition) -> list[str]:
    out = []
    for s in (edition_slice(ed.edition_count), era_slice(ed.year),
              language_slice(ed.language)):
        if s:
            out.append(s)
    return out


async def draw_sample(client: PoliteClient, store: Store, sample_n: int,
                      year_from: int, year_to: int,
                      sources: tuple[str, ...] | list[str] = ("ia", "gb"),
                      query_filter: str | None = None,
                      max_pages: int = 10) -> list[Edition]:
    """Random OL sample within a publish-year window; keyed on first valid ISBN13.

    Fetches edition_count + language (M3.2 rarity slices); `query_filter` is
    appended verbatim to the OL query (e.g. "language:por")."""
    editions: list[Edition] = []
    seen: set[str] = set()
    q = f"publish_year:[{year_from} TO {year_to}]"
    if query_filter:
        q += f" {query_filter}"
    for page in range(max_pages):
        if len(editions) >= sample_n:
            break
        resp = await client.get(SAMPLE_URL, {
            "q": q,
            "fields": "isbn,title,author_name,first_publish_year,edition_count,language",
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
                languages = d.get("language") or []
                editions.append(Edition(
                    work_key=isbn13, isbn13=isbn13, isbn10=norm[1],
                    title=d.get("title"),
                    author=", ".join(d.get("author_name") or []) or None,
                    year=d.get("first_publish_year"),
                    edition_count=d.get("edition_count"),
                    language=languages[0] if languages else None,
                    origin_note="survey"))
                store.upsert_edition(editions[-1])
                for src in sources:
                    store.enqueue(isbn13, src)
                if len(editions) >= sample_n:
                    break
            if len(editions) >= sample_n:
                break
    return editions


def decide(store: Store, ed: Edition) -> Classification:
    statuses = {r["source"]: r["status"] for r in store.conn.execute(
        "SELECT source, status FROM source_hits WHERE work_key=?", (ed.work_key,))}
    return classify_edition(ed, store.surrogates_for(ed.work_key),
                            store.rarity_for(ed.work_key), statuses)


def summarize(store: Store, editions: list[Edition]) -> dict:
    """% of sampled editions with no accessible digital surrogate, per rarity slice.

    UNKNOWN rows (source down) are excluded from every slice and the headline;
    they are reported separately with their own count."""
    rows = [(ed, decide(store, ed)) for ed in editions]
    decided = [(ed, c) for ed, c in rows if c.cls.value != "UNKNOWN"]
    unknown = len(rows) - len(decided)

    def cell(name: str, subset: list[tuple[Edition, Classification]]) -> dict:
        n = len(subset)
        k = sum(1 for _, c in subset if c.cls.value != "GREEN")
        lo, hi = wilson_ci(k, n)
        return {"n": n, "no_surrogate": k,
                "pct": round(100 * k / n, 1) if n else None,
                "wilson95": [round(lo, 3), round(hi, 3)],
                "thin": n < THIN_SAMPLE_N}

    slices: dict[str, dict] = {"all": cell("all", decided)}
    for name in SLICE_ORDER[1:]:
        slices[name] = cell(name, [(ed, c) for ed, c in decided
                                   if name in slices_for(ed)])
    return {"sample": len(rows), "decided": len(decided), "unknown": unknown,
            "slices": slices}


def render(report: dict) -> str:
    lines = [f"# lastcopy survey (sample={report['sample']}, "
             f"decided={report['decided']}, unknown={report['unknown']})", "",
             "Headline pct is over decided rows only; UNKNOWN rows (source down) "
             "are reported separately and never counted as no-surrogate.",
             "",
             "| slice | n | no-surrogate | pct | Wilson 95% CI | flags |",
             "|---|---|---|---|---|---|"]
    for name in SLICE_ORDER:
        s = report["slices"][name]
        if not s["n"]:
            lines.append(f"| {name} | 0 | — | — | — | |")
            continue
        flags = "thin-sample" if s["thin"] else ""
        lo, hi = s["wilson95"]
        lines.append(f"| {name} | {s['n']} | {s['no_surrogate']} | {s['pct']}% "
                     f"| [{lo*100:.1f}%, {hi*100:.1f}%] | {flags} |".rstrip())
    lines += ["", f"- thin-sample flag = cell n<{THIN_SAMPLE_N}",
              "- era buckets: pre-1927 / 1927–1969 / 1970+; "
              "edition_count buckets: 1 / 2–3 / ≥4; language: eng vs non-eng"]
    return "\n".join(lines)


def write_rows(path: str | Path, store: Store, editions: list[Edition]) -> None:
    """The citable per-row dataset (CC0 per NOTICE): one row per sampled edition,
    class + rule included, UNKNOWN rows kept with their class."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(ROWS_HEADER)
        for ed in editions:
            statuses = {r["source"]: r["status"] for r in store.conn.execute(
                "SELECT source, status FROM source_hits WHERE work_key=?",
                (ed.work_key,))}
            surrogates = store.surrogates_for(ed.work_key)
            c = decide(store, ed)
            w.writerow([ed.work_key, ed.isbn13, ed.title, ed.author, ed.year,
                        ed.edition_count, ed.language,
                        ",".join(sorted(statuses)),
                        ";".join(f"{s.provider}:{s.access.value}" for s in surrogates),
                        c.cls.value, c.rationale.get("rule")])
