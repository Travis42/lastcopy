"""M6 — digital-rescue sources: Project Gutenberg + Gallica/BnF bulk
(SPEC-M6-RESCUE).

Phase A (Gutenberg, keyless, bulk-first): ``fetch-gutenberg`` downloads the
pg_catalog.csv (21.2MB, ~90k rows, NO ISBN column) resumably; the matcher
joins the workset by (normalized title, first-author-surname, year +-2);
ambiguous matches resolve via Gutendex ``?isbn=`` (ISBN -> PG book id, no
key) — a Gutendex ISBN hit is an authoritative rescue.

Phase B (Gallica/BnF, keyless, two-OAI harvest + offline join): every live
BnF ISBN API is broken (research/2026-10-03), so the only reliable route is
bulk.  Harvest 1 (OAI-NUM, set ``gallica``) yields gallica-ark -> cb-ark
pairs via ``dc:relation``; Harvest 2 (OAI-CAT, set
``catalogue:edition:livres``) yields cb-ark -> ISBN (dc identifiers carry
ISBNs).  ``parse-gallica`` joins the chain offline against the workset.

Rules: PG and Gallica hits => NT, custody open (same semantics as IA/HT —
implemented in m5.assign_status / m5.assign_custody via the pg_id /
gallica_ark columns).  A browser User-Agent is MANDATORY for BnF endpoints
(the default curl/httpx UA gets 403); <=1 rps; gzip storage; per-page
checkpoints (<=10k records each) make the ~35h harvest resumable.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from . import m4, m5
from .isbn import normalize_isbn

PG_CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
GUTENDEX_URL = "https://gutendex.com/books/"   # ?isbn=... (trailing slash required)
PG_MIN_INTERVAL = 1.0                          # <=1 req/s etiquette
PG_MAX_RETRIES = 3

# Live OAI endpoints (research/2026-10-03): oai.bnf.fr/oai2.php is DEAD (404).
OAI_ENDPOINTS = {
    "num": {"url": "http://oai.bnf.fr/oai2/OAIHandler", "set": "gallica",
            "pairs": "num_pairs.tsv.gz"},
    "cat": {"url": "http://catoai.bnf.fr/oai2/OAIHandler",
            "set": "catalogue:edition:livres", "pairs": "cat_pairs.tsv.gz"},
}
# BnF 403s the default UA — a real browser UA is mandatory.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 "
              "Firefox/128.0")
OAI_HEADERS = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
OAI_MIN_INTERVAL = 1.0                         # <=1 rps etiquette
OAI_MAX_RETRIES = 3                            # backoff on 403/429/503
CHECKPOINT_EVERY_RECORDS = 10_000              # SPEC: per-10k checkpoints

_ARTICLES = {"the", "a", "an", "la", "le", "les", "un", "une", "der", "die",
             "das", "ein", "eine", "il", "lo", "gli", "el", "los", "las"}
_ARK_RE = re.compile(r"ark:/12148/(\w+)")
_ISBN_FIELD_RE = re.compile(r"^\s*ISBN\s+([0-9Xx\-]+)")


# ------------------------------------------------------------------ phase A
def fetch_gutenberg(data_dir: str | Path = "data/gutenberg",
                    *, url: str = PG_CATALOG_URL, timeout: int = 0) -> dict:
    """Download pg_catalog.csv (resumable curl -C -, IPv4) into
    ``data/gutenberg/``; integrity = parseable CSV + row count printed.
    Already-complete files are re-verified and skipped."""
    dest = Path(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "pg_catalog.csv"
    stats = {"file": str(target), "downloaded": False, "rows": None}
    rc = subprocess.call(
        ["curl", "-C", "-", "-4", "-L", "--fail", "--retry", "3",
         "-A", BROWSER_UA, "-o", str(target), url], timeout=timeout or None)
    if rc != 0:
        stats["error"] = f"curl rc={rc}"
        return stats
    try:
        with open(target, newline="", encoding="utf-8-sig",
                  errors="replace") as fh:
            reader = csv.DictReader(fh)
            # integrity: recognizable pg_catalog header + parseable rows
            required = {"Text#", "Type", "Title", "Authors"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise csv.Error(
                    f"header lacks pg_catalog columns: {reader.fieldnames}")
            rows = sum(1 for _ in reader)
    except (OSError, csv.Error) as exc:
        stats["error"] = f"csv parse failed: {exc}"
        return stats
    stats.update({"downloaded": True, "rows": rows})
    print(f"[gutenberg] {target}: {rows:,} catalog rows (parseable CSV ok)")
    return stats


def norm_title(s: str | None) -> str:
    """Case/punctuation/accent-insensitive title key with leading articles
    dropped ('The Winter's Tale' == \"winter s tale\")."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"['’]", "", s.lower())
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    words = s.split()
    while words and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def author_surname(s: str | None) -> str:
    """First author's surname, normalized: handles both 'Austen, Jane' /
    'Austen, Jane, 1775-1817' (PG form) and 'Jane Austen' (free form);
    ';' separates additional authors (PG) — only the first is used."""
    if not s:
        return ""
    first = re.split(r"[;]", str(s))[0].strip()
    if not first:
        return ""
    if "," in first:
        first = first.split(",")[0]
    else:
        first = first.split()[-1] if first.split() else ""
    first = unicodedata.normalize("NFKD", first)
    first = "".join(c for c in first if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", first.lower())


def load_pg_catalog(path: str | Path) -> list[dict]:
    """Stream pg_catalog.csv -> [{id, title, authors, year?}] for Type=Text
    rows (Sound etc. excluded).  ``year`` is absent in the real catalog
    (Issued = PG release date, not publication year) — the matcher applies
    the +-2 window only when both sides carry a year."""
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if (row.get("Type") or "").strip().lower() != "text":
                continue
            out.append({"id": (row.get("Text#") or "").strip(),
                        "title": row.get("Title") or "",
                        "authors": row.get("Authors") or "",
                        "language": (row.get("Language") or "").strip()})
    return out


def match_pg(title: str | None, author: str | None, year: int | None,
             pg_rows: list[dict], *, year_window: int = 2) -> list[str]:
    """PG Text#s matching (normalized title, first-author-surname, year+-2).
    Year criterion applies only when BOTH sides carry a year (the bulk
    catalog has none).  Returns ALL matches — >1 = ambiguous (caller
    resolves via Gutendex)."""
    t_key = norm_title(title)
    s_key = author_surname(author)
    if not t_key or not s_key:
        return []
    hits = []
    for row in pg_rows:
        if norm_title(row.get("title")) != t_key:
            continue
        if author_surname(row.get("authors")) != s_key:
            continue
        ry, wy = row.get("year"), year
        if ry is not None and wy is not None and abs(int(ry) - int(wy)) \
                > year_window:
            continue
        if row.get("id"):
            hits.append(row["id"])
    return hits


def _gutendex_lookup(isbn13: str, *, get, sleep, max_retries: int,
                     min_interval: float) -> str | None:
    """Gutendex ?isbn= -> PG book id (authoritative rescue), or None.
    Response shape: {"count": n, "results": [{"id": 1342, ...}]}."""
    data = m5._request_json(get, {"isbn": isbn13}, sleep, max_retries,
                            min_interval, url=GUTENDEX_URL)
    if not data or not data.get("results"):
        return None
    rid = data["results"][0].get("id")
    return str(rid) if rid is not None else None


def enrich_gutenberg(conn, catalog: str | Path, *,
                     get=None, sleep=time.sleep,
                     min_interval: float = PG_MIN_INTERVAL,
                     max_retries: int = PG_MAX_RETRIES,
                     gutendex_budget: int = 5_000,
                     progress_every: int = 0) -> dict:
    """Match the workset against the PG bulk catalog; set ``pg_id`` on
    unique matches and on Gutendex-confirmed ambiguous ones (budget-capped).
    Marks 'pg' checked on every row (idempotent; pg_id already set -> skip).
    NT/custody land later via the assign-status chain."""
    m5.ensure_schema(conn)
    if get is None:
        get = m5._http_get
    pg_rows = load_pg_catalog(catalog)
    by_title: dict[str, list[dict]] = {}
    for row in pg_rows:
        k = norm_title(row["title"])
        if k:
            by_title.setdefault(k, []).append(row)
    todo = conn.execute(
        """SELECT w.isbn13, c.title, c.year, wr.author_keys
           FROM enrich_workset w
           JOIN enrich_status s ON s.isbn13 = w.isbn13
           LEFT JOIN candidates c ON c.isbn13 = w.isbn13
           LEFT JOIN works_ref wr ON wr.work_key = w.work_key
           WHERE s.pg_id IS NULL AND c.title IS NOT NULL
           ORDER BY w.isbn13 ASC""").fetchall()
    unique = gutendex = gutendex_hits = 0
    n = 0
    for r in todo:
        n += 1
        if progress_every and n % progress_every == 0:
            print(f"[gutenberg] {n:,}/{len(todo):,} rows "
                  f"(unique={unique}, gutendex={gutendex_hits})",
                  file=sys.stderr, flush=True)
        author = m4.resolve_authors(conn, r["author_keys"])
        cands = by_title.get(norm_title(r["title"]), [])
        matches = match_pg(r["title"], author, r["year"], cands)
        if len(matches) == 1:
            conn.execute("UPDATE enrich_status SET pg_id=? WHERE isbn13=?",
                         (matches[0], r["isbn13"]))
            conn.commit()
            unique += 1
        elif len(matches) > 1 and gutendex < gutendex_budget:
            # ambiguous -> Gutendex ISBN resolution (authoritative)
            gutendex += 1
            pg_id = _gutendex_lookup(r["isbn13"], get=get, sleep=sleep,
                                     max_retries=max_retries,
                                     min_interval=min_interval)
            if pg_id is not None:
                conn.execute("UPDATE enrich_status SET pg_id=? WHERE isbn13=?",
                             (pg_id, r["isbn13"]))
                conn.commit()
                gutendex_hits += 1
    m5._mark_checked(conn, "pg")
    return {"pg_rows": len(pg_rows), "rows_scanned": n, "unique": unique,
            "gutendex_queries": gutendex, "gutendex_hits": gutendex_hits,
            "pg_id_set": _n(conn, "pg_id IS NOT NULL")}


def _n(conn, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) c FROM enrich_status WHERE {where}"
                        ).fetchone()["c"]


# ------------------------------------------------------------------ phase B
def _oai_get(url: str, params: dict, headers: dict | None = None):
    """Default (real) OAI transport: httpx sync, IPv4-forced, browser UA
    (MANDATORY — BnF 403s default clients), gzip accepted."""
    import httpx
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(timeout=60, follow_redirects=True,
                      transport=transport) as client:
        return client.get(url, params=params, headers=headers or OAI_HEADERS)


def _oai_request(get, url: str, params: dict, sleep, max_retries: int,
                 min_interval: float, headers: dict | None = None):
    """OAI GET with _sru_request-style discipline: min-interval pacing,
    exponential backoff on 403/429/503 AND transport errors, then None."""
    import httpx
    attempt = 0
    while True:
        sleep(min_interval)
        try:
            resp = get(url, params, headers=headers or OAI_HEADERS)
        except httpx.HTTPError:
            if attempt < max_retries - 1:
                sleep(2.0 ** attempt)
                attempt += 1
                continue
            return None
        if resp.status_code in (403, 429, 503) and attempt < max_retries - 1:
            sleep(2.0 ** attempt)
            attempt += 1
            continue
        if getattr(resp, "ok", 200 <= resp.status_code < 300):
            return resp.text
        return None


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_oai_page(text: str) -> tuple[list[tuple[str, str]], str | None, int]:
    """One ListRecords oai_dc page -> (pairs, resumptionToken|None, n_records).

    NUM harvest pairs (gallica_ark, cb_ark): dc:identifier holds the
    digitized object ark (gallica.bnf.fr/ark:/12148/<id>), dc:relation the
    catalogue notice (catalogue.bnf.fr/ark:/12148/cbXXXX).
    CAT harvest pairs (cb_ark, isbn): dc:identifier holds
    'ISBN <n>' alongside the cb ark (verified shape, research 2026-10-03).
    A record yields a pair only when BOTH sides are present."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], None, 0
    token = None
    pairs: list[tuple[str, str]] = []
    n_records = 0
    for el in root.iter():
        if _xml_local(el.tag) == "resumptionToken":
            txt = (el.text or "").strip()
            token = txt or None
    for rec in root.iter():
        if _xml_local(rec.tag) != "record":
            continue
        n_records += 1
        identifiers: list[str] = []
        relations: list[str] = []
        for el in rec.iter():
            t = _xml_local(el.tag)
            if t == "identifier" and el.text:
                identifiers.append(el.text.strip())
            elif t == "relation" and el.text:
                relations.append(el.text.strip())
        gallica_ark = cb_ark = None
        isbn: str | None = None
        for ident in identifiers:
            for ark in _ARK_RE.findall(ident):
                if ark.startswith("cb"):
                    cb_ark = cb_ark or ark
                elif "gallica" in ident or ark.startswith(("bpt6", "btv1")):
                    gallica_ark = gallica_ark or ark
            m = _ISBN_FIELD_RE.match(ident)
            if m:
                norm = normalize_isbn(m.group(1))
                if norm:
                    isbn = isbn or norm[0]
        # NUM shape: gallica identifier + cb relation
        if gallica_ark:
            for rel in relations:
                for ark in _ARK_RE.findall(rel):
                    if ark.startswith("cb"):
                        pairs.append((gallica_ark, ark))
                        break
        # CAT shape: cb + ISBN in the same record
        elif cb_ark and isbn:
            pairs.append((cb_ark, isbn))
    return pairs, token, n_records


def harvest_gallica(stage: str, data_dir: str | Path = "data/gallica", *,
                    get=None, sleep=time.sleep,
                    min_interval: float = OAI_MIN_INTERVAL,
                    max_retries: int = OAI_MAX_RETRIES,
                    max_pages: int | None = None,
                    checkpoint_every: int = CHECKPOINT_EVERY_RECORDS) -> dict:
    """One OAI harvest stage ('num' | 'cat'): walk ListRecords
    resumptionToken pages at <=1 rps with a browser UA, append extracted
    ark pairs to a gzip TSV, checkpoint (token + counts) per page (<=10k
    records), stop at ``max_pages`` (canary/slicing).  Resumes from the
    checkpoint on rerun; a finished harvest (done marker) is skipped with
    zero requests."""
    if stage not in OAI_ENDPOINTS:
        raise ValueError(f"stage must be one of {sorted(OAI_ENDPOINTS)}")
    if get is None:
        get = _oai_get
    ep = OAI_ENDPOINTS[stage]
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    pairs_path = base / ep["pairs"]
    ckpt_path = base / f"{stage}.ckpt"
    done_path = base / f"{stage}.done"
    if done_path.exists():
        return {"stage": stage, "skipped_done": True, "pages": 0,
                "records": 0, "pairs": 0, "done": True}
    token: str | None = None
    pages = records = pairs = 0
    if ckpt_path.exists():
        try:
            ck = json.loads(ckpt_path.read_text(encoding="utf-8"))
            token, pages, records, pairs = (ck["token"], ck["pages"],
                                            ck["records"], ck["pairs"])
        except (json.JSONDecodeError, KeyError, ValueError):
            token = None   # corrupt checkpoint -> restart from scratch
    resumed = token is not None
    done = False
    last_ckpt_records = records
    pages_run = 0
    while True:
        if token:
            params = {"verb": "ListRecords", "resumptionToken": token}
        else:
            params = {"verb": "ListRecords", "metadataPrefix": "oai_dc",
                      "set": ep["set"]}
        text = _oai_request(get, ep["url"], params, sleep, max_retries,
                            min_interval)
        if text is None:
            break          # failed page -> checkpoint stays; retried next run
        page_pairs, next_token, n_recs = parse_oai_page(text)
        pages += 1
        records += n_recs
        pairs += len(page_pairs)
        if page_pairs:
            with gzip.open(pairs_path, "at", encoding="utf-8") as fh:
                fh.write("".join(f"{a}\t{b}\n" for a, b in page_pairs))
        # checkpoint per page, and always before a >=10k-records boundary
        ckpt_path.write_text(json.dumps(
            {"token": next_token, "pages": pages, "records": records,
             "pairs": pairs}), encoding="utf-8")
        if (records - last_ckpt_records >= checkpoint_every
                or next_token is None):
            last_ckpt_records = records
            print(f"[gallica:{stage}] page {pages}: {records:,} records, "
                  f"{pairs:,} pairs (ckpt token={'yes' if next_token else 'end'})",
                  file=sys.stderr, flush=True)
        if next_token is None:
            done = True
            done_path.write_text(m5.now() + "\n", encoding="utf-8")
            break
        token = next_token
        pages_run += 1
        if max_pages is not None and pages_run >= max_pages:
            break
    return {"stage": stage, "skipped_done": False, "pages": pages,
            "records": records, "pairs": pairs, "done": done,
            "resumed": resumed}


def parse_gallica(conn, num_pairs: str | Path, cat_pairs: str | Path,
                  progress_every: int = 0) -> dict:
    """Offline join gallica-ark -> cb-ark -> ISBN against the workset ->
    ``gallica_ark`` column (first ark wins; ISBN-10 folds to 13).
    Idempotent (already-set rows are skipped); marks 'gallica' checked."""
    m5.ensure_schema(conn)
    workset = {r["isbn13"]
               for r in conn.execute(
                   "SELECT isbn13 FROM enrich_workset "
                   "WHERE isbn13 NOT IN (SELECT isbn13 FROM enrich_status "
                   "WHERE gallica_ark IS NOT NULL)")}
    cb2gallica: dict[str, str] = {}
    n_num = 0
    with gzip.open(num_pairs, "rt", encoding="utf-8") as fh:
        for line in fh:
            n_num += 1
            gallica, cb = line.rstrip("\n").split("\t")[:2]
            cb2gallica.setdefault(cb, gallica)
    matched: dict[str, str] = {}
    n_cat = n_cat_isbn = 0
    with gzip.open(cat_pairs, "rt", encoding="utf-8") as fh:
        for line in fh:
            n_cat += 1
            cb, raw = line.rstrip("\n").split("\t")[:2]
            gallica = cb2gallica.get(cb)
            if not gallica:
                continue
            norm = normalize_isbn(raw)
            if not norm:
                continue
            n_cat_isbn += 1
            if norm[0] in workset and norm[0] not in matched:
                matched[norm[0]] = gallica
            if progress_every and n_cat % progress_every == 0:
                print(f"[gallica:join] {n_cat:,} cat rows, "
                      f"{len(matched):,} workset matches", file=sys.stderr,
                      flush=True)
    if matched:
        conn.executemany(
            "UPDATE enrich_status SET gallica_ark=? WHERE isbn13=? "
            "AND gallica_ark IS NULL", [(g, i) for i, g in matched.items()])
        conn.commit()
    m5._mark_checked(conn, "gallica")
    return {"num_pairs": n_num, "cat_pairs": n_cat,
            "cat_isbn_pairs": n_cat_isbn, "gallica_ark_set":
            _n(conn, "gallica_ark IS NOT NULL"), "matched": len(matched)}
