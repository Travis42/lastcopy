"""lastcopy CLI (stdlib argparse): ingest / enrich / classify / report (+ survey, confirm stubs)."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from tqdm import tqdm

from . import __version__
from .classify import classify_edition
from .isbn import bib_work_key, normalize_isbn
from .models import Classification, Cls, Edition, HitStatus, SourceName
from .net import PoliteClient
from .store import Store
from .sources import gbooks, ia, ol, wikidata
from .sources.gbooks import KeyRequiredError

SOURCE_CHECKS = {"ol": ol.check, "ia": ia.check, "wd": wikidata.check,
                 "gb": gbooks.check}
STUB_SOURCES = {"ht": "HathiTrust", "loc": "LoC SRU"}
DEFAULT_KEYLESS = "ol,ia,wd"


# ---------------------------------------------------------------- ingest
def cmd_ingest(args) -> int:
    store = Store(args.db)
    run_id = store.start_run("ingest")
    rows = list(csv.DictReader(open(args.csv, newline="", encoding="utf-8-sig")))
    n_isbn = n_bib = n_invalid = 0
    for raw in rows:
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        title, author = row.get("title"), row.get("author") or ""
        year = _to_year(row.get("year"))
        isbn_raw = row.get("isbn") or row.get("isbn13") or row.get("isbn10") or ""
        norm = normalize_isbn(isbn_raw) if isbn_raw else None
        ed = None
        if norm and not args.bib_mode:
            isbn13, isbn10 = norm
            ed = Edition(work_key=isbn13, isbn13=isbn13, isbn10=isbn10,
                         title=title, author=author, year=year,
                         publisher=row.get("publisher"),
                         imprint_place=row.get("place") or row.get("imprint_place"),
                         language=row.get("language"), origin_note="csv")
            n_isbn += 1
            for src in DEFAULT_KEYLESS.split(","):
                store.enqueue(ed.work_key, src)
        elif isbn_raw and not norm and not args.bib_mode:
            n_invalid += 1  # invalid checksum/length -> falls to bib-stub path
        if (not norm or args.bib_mode) and title:
            note = "bib-mode" if args.bib_mode else (
                "bib-stub:invalid-isbn" if isbn_raw else "bib-stub:no-isbn")
            ed = Edition(work_key=bib_work_key(title, author, year),
                         title=title, author=author, year=year,
                         publisher=row.get("publisher"),
                         imprint_place=row.get("place") or row.get("imprint_place"),
                         language=row.get("language"), origin_note=note)
            n_bib += 1
            store.enqueue(ed.work_key, "bib-stub")  # D1: queued only, never resolved in M1
        if ed is None:
            continue  # nothing usable in this row
        store.upsert_edition(ed)
    store.finish_run(run_id, {"rows": len(rows), "isbn": n_isbn, "bib_stub": n_bib,
                              "invalid_isbn": n_invalid})
    print(f"ingested {len(rows)} rows: {n_isbn} ISBN-keyed, {n_bib} bib-stub "
          f"({n_invalid} invalid-ISBN fallbacks)")
    store.close()
    return 0


# ---------------------------------------------------------------- enrich
async def _enrich(args) -> int:
    store = Store(args.db)
    run_id = store.start_run("enrich")

    # Backfill (SPEC M3.3): re-enqueue unavailable rows before the normal pass.
    if getattr(args, "retry_unavailable", False):
        pairs = store.retry_unavailable()
        by_src: Counter = Counter(src for _, src in pairs)
        print(f"retry-unavailable: re-enqueued {len(pairs)} source_hits row(s) "
              + ", ".join(f"{s}={n}" for s, n in sorted(by_src.items()))
              + " (cached error responses evicted; queue resumable)")

    # Default source set (SPEC M3.1): ol,ia,wd + gb when a key resolves.
    if args.source is None:
        sources = DEFAULT_KEYLESS.split(",")
        if gbooks.resolve_key():
            sources.append("gb")
        else:
            print("note: gb omitted from default sources (no Google Books key; "
                  f"set {gbooks.KEY_ENV} or create {gbooks.KEY_FILE})",
                  file=sys.stderr)
    else:
        sources = [s.strip() for s in args.source.split(",") if s.strip()]

    for s in sources:
        if s in STUB_SOURCES:
            print(f"error: source '{s}' ({STUB_SOURCES[s]}) requires a key — "
                  "scaffolded only, wiring lands in M3 (SPEC D2)", file=sys.stderr)
            store.close()
            return 2
        if s not in SOURCE_CHECKS:
            print(f"error: unknown source '{s}'", file=sys.stderr)
            store.close()
            return 2
    # explicit --source gb without a key raises (never silently skips an explicit ask)
    if "gb" in sources and not gbooks.resolve_key():
        store.close()
        raise gbooks.no_key_error()

    # gb rows are enqueued at enrich time (not ingest) so ingest stays key-agnostic
    if "gb" in sources:
        for ed in store.all_editions():
            if ed.isbn13:
                store.enqueue(ed.work_key, "gb")

    client = PoliteClient(store)
    sem = asyncio.Semaphore(args.workers)
    totals: Counter = Counter()
    editions = store.all_editions()

    # OL must run before IA (IA's oclc: fallback reads OL evidence); WD/GB order-free.
    for src in [s for s in ("ol", "ia", "wd", "gb") if s in sources]:
        check = SOURCE_CHECKS[src]
        items = store.pending(src)
        if not items:
            continue

        async def one(row):
            async with sem:
                work_key = row["work_key"]
                ed = next((e for e in editions if e.work_key == work_key), None)
                if ed is None:
                    store.queue_mark(work_key, src, "error", "edition vanished")
                    return
                try:
                    out = await check(ed, client, store)
                    hit, surrogates = out if isinstance(out, tuple) else (out, [])
                    store.save_hit(hit)
                    if surrogates:
                        store.save_surrogates(work_key, surrogates)
                    store.queue_mark(work_key, src, "done")
                    totals[hit.status.value] += 1
                except KeyRequiredError:
                    raise
                except Exception as exc:  # leave pending -> resumable after crash
                    store.queue_mark(work_key, src, "pending", str(exc)[:500])
                    totals["error"] += 1

        for row in tqdm(items, desc=f"enrich:{src}", unit="ed"):
            await one(row)

    await client.aclose()
    store.finish_run(run_id, {"sources": sources, **dict(totals)})
    print(f"enrich done: {dict(totals)} (queue resumable; cache TTL 30d)")
    store.close()
    return 0


def cmd_enrich(args) -> int:
    return asyncio.run(_enrich(args))


# ---------------------------------------------------------------- classify
def cmd_classify(args) -> int:
    store = Store(args.db)
    run_id = store.start_run("classify")
    counts: Counter = Counter()
    for ed in store.all_editions():
        surrogates = store.surrogates_for(ed.work_key)
        rarity = store.rarity_for(ed.work_key)
        statuses = {
            r["source"]: r["status"]
            for r in store.conn.execute(
                "SELECT source, status FROM source_hits WHERE work_key=?", (ed.work_key,))
        }
        c = classify_edition(ed, surrogates, rarity, statuses)
        store.save_classification(c)
        counts[c.cls.value] += 1
    store.finish_run(run_id, dict(counts))
    print("classified: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    store.close()
    return 0


# ---------------------------------------------------------------- report
def _work_group(store: Store, ed: Edition) -> str:
    row = store.conn.execute(
        "SELECT evidence_json FROM source_hits WHERE work_key=? AND source='ol'",
        (ed.work_key,)).fetchone()
    if row:
        try:
            wk = json.loads(row["evidence_json"]).get("ol_work_key")
            if wk:
                return wk
        except json.JSONDecodeError:
            pass
    key = f"{(ed.title or '').lower().strip()}|{(ed.author or '').lower().strip()}"
    return key or ed.work_key


def _evidence(store: Store, work_key: str, cls_row: Classification) -> list[str]:
    urls = list(cls_row.evidence_urls)
    for s in store.surrogates_for(work_key):
        if s.url not in urls:
            urls.append(s.url)
    return urls


def cmd_report(args) -> int:
    store = Store(args.db)
    run_id = store.start_run("report")
    editions = {e.work_key: e for e in store.all_editions()}
    classes = {c.work_key: c for c in store.all_classifications()}
    order = [Cls.RED.value, Cls.RED_UNVERIFIED.value, Cls.AMBER.value,
             Cls.UNKNOWN.value, Cls.GREEN.value]
    entries = []
    for wk, c in classes.items():
        ed = editions.get(wk)
        title = ed.title if ed else None
        author = ed.author if ed else None
        if not title or not author:  # bare-ISBN lot rows: borrow bib data from OL evidence
            row = store.conn.execute(
                "SELECT evidence_json FROM source_hits WHERE work_key=? AND source='ol'",
                (wk,)).fetchone()
            if row:
                try:
                    ev = json.loads(row["evidence_json"])
                    title = title or ev.get("title")
                    author = author or ", ".join(ev.get("authors") or []) or None
                except json.JSONDecodeError:
                    pass
        entries.append({
            "work_key": wk, "isbn13": ed.isbn13 if ed else None,
            "title": title, "author": author,
            "year": ed.year if ed else None,
            "cls": c.cls.value, "rule": c.rationale.get("rule"),
            "evidence": _evidence(store, wk, c),
        })
    entries.sort(key=lambda e: (order.index(e["cls"]), e["title"] or ""))
    counts = Counter(e["cls"] for e in entries)

    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(_work_group(store, editions[e["work_key"]]), []).append(e)

    if args.csv:
        _write_csv(args.csv, entries)
    md = _render_md(entries, groups, counts)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    print(md)
    store.finish_run(run_id, {"entries": len(entries), "counts": dict(counts)})
    store.close()
    return 0


def _write_csv(path: str, entries: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["work_key", "isbn13", "title", "author", "year", "class",
                    "rule", "evidence_urls"])
        for e in entries:
            w.writerow([e["work_key"], e["isbn13"], e["title"], e["author"], e["year"],
                        e["cls"], e["rule"], ";".join(e["evidence"])])


def _render_md(entries, groups, counts) -> str:
    lines = ["# lastcopy report", ""]
    lines.append(f"{len(entries)} editions classified: "
                 + ", ".join(f"{k}={counts[k]}" for k in
                             [c.value for c in Cls] if counts.get(k)) + "")
    lines.append("")
    red = [e for e in entries if e["cls"] in (Cls.RED.value, Cls.RED_UNVERIFIED.value)]
    lines.append(f"## RED list — act before shipping ({len(red)})")
    lines.append("")
    lines.append("| class | title | author | year | isbn | rationale | evidence |")
    lines.append("|---|---|---|---|---|---|---|")
    for e in red:
        lines.append(f"| {e['cls']} | {e['title']} | {e['author']} | {e['year']} "
                     f"| {e['isbn13']} | {e['rule']} | {'; '.join(e['evidence']) or '—'} |")
    lines.append("")
    lines.append("## Roll-up per work")
    lines.append("")
    lines.append("| work | editions | classes | evidence |")
    lines.append("|---|---|---|---|")
    for g, es in sorted(groups.items()):
        ev: list[str] = []
        for e in es:
            for u in e["evidence"]:
                if u not in ev:
                    ev.append(u)
        lines.append(f"| {(es[0]['title'] or g)} — {(es[0]['author'] or '?')} "
                     f"| {len(es)} | {','.join(e['cls'] for e in es)} "
                     f"| {'; '.join(ev[:5]) or '—'} |")
    lines.append("")
    lines.append("## Counts by class")
    lines.append("")
    for c in Cls:
        lines.append(f"- {c.value}: {counts.get(c.value, 0)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- survey (M3.2)
def cmd_survey(args) -> int:
    from . import survey as survey_mod

    # Survey default sources = ia,gb (SPEC M3.2; wd optional ~3x wall cost).
    if args.source is None:
        sources = ["ia"]
        if gbooks.resolve_key():
            sources.append("gb")
        else:
            print("note: gb omitted from survey sources (no Google Books key; "
                  f"set {gbooks.KEY_ENV} or create {gbooks.KEY_FILE})",
                  file=sys.stderr)
    else:
        sources = [s.strip() for s in args.source.split(",") if s.strip()]
        bad = [s for s in sources if s not in ("ia", "wd", "gb")]
        if bad:
            print(f"error: survey sources must be ia,wd,gb — got {','.join(bad)}",
                  file=sys.stderr)
            return 2
        # explicit --source gb without a key raises (never silently skips an ask)
        if "gb" in sources and not gbooks.resolve_key():
            raise gbooks.no_key_error()

    async def _run() -> dict:
        store = Store(args.db)
        run_id = store.start_run("survey")
        client = PoliteClient(store)
        editions = await survey_mod.draw_sample(
            client, store, args.sample, args.from_year, args.to_year,
            sources=sources, query_filter=args.survey_filter)
        totals = await _enrich_survey(client, store, sources)
        await client.aclose()
        if args.rows:
            survey_mod.write_rows(args.rows, store, editions)
        report = survey_mod.summarize(store, editions)
        report["enrich"] = dict(totals)
        store.finish_run(run_id, report)
        store.close()
        return report

    async def _enrich_survey(client, store, sources):
        totals: Counter = Counter()
        editions = {e.work_key: e for e in store.all_editions()}
        for src in sources:
            check = SOURCE_CHECKS[src]
            for row in tqdm(store.pending(src), desc=f"survey:{src}", unit="ed"):
                ed = editions.get(row["work_key"])
                if ed is None:
                    continue
                try:
                    out = await check(ed, client, store)
                    hit, surrogates = out if isinstance(out, tuple) else (out, [])
                    store.save_hit(hit)
                    if surrogates:
                        store.save_surrogates(ed.work_key, surrogates)
                    store.queue_mark(ed.work_key, src, "done")
                    totals[hit.status.value] += 1
                except KeyRequiredError:
                    raise
                except Exception as exc:  # stay resumable; row classifies UNKNOWN
                    store.queue_mark(ed.work_key, src, "error", str(exc)[:500])
                    totals["error"] += 1
        return totals

    report = asyncio.run(_run())
    text = survey_mod.render(report)
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_confirm(args) -> int:
    print("confirm is M3 (needs OCLC WSKey / manual annotation workflow)", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- M4: THE LIST
def cmd_ingest_works(args) -> int:
    from . import m4

    if args.works or args.authors:
        works, authors = args.works, args.authors
    else:
        d = Path(args.data_dir)
        works = sorted(d.glob("ol_dump_works_*.txt.gz"))
        authors = sorted(d.glob("ol_dump_authors_*.txt.gz"))
        if not works or not authors:
            print(f"error: no ol_dump_works_*.txt.gz / ol_dump_authors_*.txt.gz "
                  f"under {d} (or pass --works/--authors explicitly)", file=sys.stderr)
            return 2
        works, authors = works[-1], authors[-1]
    conn = m4.connect(args.db)
    try:
        stats = m4.ingest_works(conn, works, authors)
    finally:
        conn.close()
    print(f"ingest-works: {stats['works']:,} works, {stats['authors']:,} authors "
          f"(from {works}, {authors})")
    return 0


def cmd_ingest_editions(args) -> int:
    from . import m4

    if bool(args.file) == bool(args.stream_url):
        print("error: pass exactly one of --file / --stream-url", file=sys.stderr)
        return 2
    conn = m4.connect(args.db)
    try:
        stats = m4.ingest_editions(conn, file=args.file, url=args.stream_url,
                                   retries=args.retries)
    finally:
        conn.close()
    print(f"ingest-editions: {stats['isbn_keyed_rows']:,} ISBN-keyed rows kept "
          f"({stats['restarts']} stream restart(s); dump never written to disk)")
    return 0


def cmd_gen_candidates(args) -> int:
    from . import m4

    conn = m4.connect(args.db)
    try:
        stats = m4.gen_candidates(conn, max_editions=args.max_editions,
                                  lang=args.lang, from_year=args.from_year,
                                  to_year=args.to_year)
    finally:
        conn.close()
    print(f"gen-candidates: {stats['candidates']:,} candidate(s) "
          f"({stats['filters']})")
    return 0


def cmd_export_list(args) -> int:
    from . import m4, m5

    if not args.csv and not args.md:
        print("error: pass at least one of --csv / --md", file=sys.stderr)
        return 2
    conn = m4.connect(args.db)
    try:
        if getattr(args, "workset", False):
            stats = m5.export_workset(conn, args.csv or
                                      str(Path(args.md).with_suffix(".csv")),
                                      args.md)
            print(f"export-list: {stats['exported']:,} workset row(s) with "
                  f"status columns -> {stats['csv']}"
                  + (f", {stats['md']}" if stats["md"] else "")
                  + (f" ({m5.PROVISIONAL_NOTE})" if stats.get("gb_pending")
                     else " (GB verification complete)"))
            return 0
        stats = m4.export_list(conn, args.top, args.csv or
                               str(Path(args.md).with_suffix(".csv")), args.md,
                               editions_dump=args.editions_dump)
    finally:
        conn.close()
    print(f"export-list: {stats['exported']:,} row(s) -> {stats['csv']}"
          + (f", {stats['md']}" if stats["md"] else ""))
    return 0


# ---------------------------------------------------------------- M5: enrichment
def cmd_backfill_oclc(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.backfill_oclc(conn, args.file, limit=args.limit)
    finally:
        conn.close()
    print(f"backfill-oclc: workset={stats['workset']:,}, "
          f"oclc backfilled={stats['oclc_backfilled']:,}")
    return 0


def cmd_enrich_ht(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.enrich_ht(conn, args.hathifile)
    finally:
        conn.close()
    print(f"enrich-ht: {stats['lines_read']:,} hathifile lines, "
          f"{stats['staged_rows']:,} staged rows, "
          f"allow={stats['ht']['allow']:,} deny={stats['ht']['deny']:,} (zero API)")
    return 0


def cmd_enrich_wikidata(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.enrich_wikidata(conn, args.results)
    finally:
        conn.close()
    print(f"enrich-wikidata: {stats['bindings']:,} bindings, "
          f"wd_fulltext=1 for {stats['wd_fulltext']:,} (offline parse)")
    return 0


def cmd_enrich_ia(args) -> int:
    from . import m4, m5

    if getattr(args, "results_dir", None) and not args.execute:
        print("error: --results-dir requires --execute (parallel executor "
              "mode, M5.8)", file=sys.stderr)
        return 2
    # parallel executor mode: read-only DB (ia_plan read, results -> files)
    conn = (m5.connect_ro(args.db) if getattr(args, "results_dir", None)
            else m4.connect(args.db))
    try:
        if args.execute:
            total = 0
            for idx in args.execute:
                try:
                    stats = m5.execute_ia_element(conn, idx,
                                                  results_dir=args.results_dir)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    conn.close()
                    return 2
                total += stats["hits"]
                print(f"enrich-ia: element {stats['element']} "
                      f"{'skipped (done)' if stats['skipped_done'] else 'executed'}, "
                      f"hits={stats['hits']}")
            print(f"enrich-ia: executed {len(args.execute)} element(s), "
                  f"{total} hit(s) recorded"
                  + (f" under {args.results_dir} (no DB writes; "
                     f"merge via merge-ia-results)" if args.results_dir
                     else ""))
        else:
            stats = m5.build_ia_plan(conn, batch=args.batch)
            print(f"enrich-ia: plan written: {stats['elements']} element(s) "
                  f"({stats['isbn_isbns']} isbn rows, "
                  f"{stats['oclc_isbns']} oclc rows; batch={args.batch}, "
                  f"arraysize={args.arraysize} for the runner)")
    finally:
        conn.close()
    return 0


def cmd_merge_ia_results(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.merge_ia_results(conn, args.results_dir)
    finally:
        conn.close()
    print(f"merge-ia-results: {stats['files']} slice file(s) -> "
          f"applied={stats['applied']}, "
          f"skipped-already-set={stats['skipped_already_set']}, "
          f"unknown-isbn={stats['unknown_isbn']} (idempotent, single writer)")
    return 0


def cmd_assign_status(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.assign_status(conn)
    finally:
        conn.close()
    print(f"assign-status: {stats['assigned']:,} row(s) -> "
          + ", ".join(f"{k}={v}" for k, v in sorted(stats["counts"].items()))
          + (f"; custody -> " if "custody" in stats else "")
          + (", ".join(f"{k}={v}" for k, v in sorted(stats["custody"].items()))
             if "custody" in stats else ""))
    return 0


def cmd_assign_custody(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.assign_custody(conn)
    finally:
        conn.close()
    print(f"assign-custody: {stats['custody_assigned']:,} row(s) -> "
          + ", ".join(f"{k}={v}" for k, v in sorted(stats["custody"].items())))
    return 0


def cmd_ocaid_sweep(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.ocaid_sweep(conn, args.file, max_records=args.max_records)
    finally:
        conn.close()
    print(f"ocaid-sweep: {stats['records_read']:,} records read, "
          f"{stats['works_with_ocaid']:,} work(s) with ocaid, "
          f"{stats['upgraded']:,} workset ISBN(s) upgraded "
          f"(ia_source='ocaid')")
    return 0


def cmd_gb_trickle(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.gb_trickle(conn, args.budget, key_file=args.key_file)
    finally:
        conn.close()
    print(f"gb-trickle: {stats['queried']} queried (budget {args.budget}) -> "
          + ", ".join(f"{k}={v}" for k, v in sorted(stats["counts"].items()))
          + "; resumable (rows with gb_status set are skipped)")
    return 0


# ------------------------------------------------------------- M5.7: holdings
def cmd_fetch_holdings_bulk(args) -> int:
    from . import m5

    try:
        stats = m5.fetch_holdings_bulk(args.institution,
                                       data_dir=args.data_dir,
                                       parts=args.parts, weeks=args.weeks)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"fetch-holdings-bulk: {args.institution} -> "
          f"{stats['downloaded']} downloaded, {stats['resumed']} resumed, "
          f"{stats['skipped_ok']} skipped (already ok), "
          f"{stats['failed']} failed (gzip-checked, resumable via curl -C -)")
    return 0 if stats["failed"] == 0 else 1


def cmd_parse_holdings_bulk(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.parse_holdings_bulk(conn, args.institution, args.file)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"parse-holdings-bulk: {args.institution} "
          f"{stats['records_read']:,} records read, "
          f"{stats['isbn_matches']:,} workset ISBN matches -> "
          f"{stats['holdings_rows']:,} holdings row(s) (idempotent)")
    return 0


def cmd_enrich_holdings(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.enrich_holdings(conn, args.institution, args.budget)
    finally:
        conn.close()
    print(f"enrich-holdings: {args.institution} queried {stats['queried']} "
          f"(budget {stats['budget']}) -> held={stats['held']}, "
          f"missed={stats['missed']}, failed={stats['failed']}; "
          "resumable (rows with holdings are skipped)")
    return 0


def cmd_assign_holdings_summary(args) -> int:
    from . import m4, m5

    conn = m4.connect(args.db)
    try:
        stats = m5.assign_holdings_summary(conn)
    finally:
        conn.close()
    print(f"assign-holdings-summary: {stats['rows']:,} row(s), "
          f"{stats['with_holdings']:,} with holdings -> "
          + ", ".join(f"{k}={v:,}"
                      for k, v in sorted(stats["by_institution"].items())))
    return 0


# ---------------------------------------------------------------- main
def _to_year(v):
    try:
        return int(str(v)[:4]) if v else None
    except ValueError:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lastcopy",
        description="Last-copy registry: flag editions with no accessible digital "
                    "surrogate and few surviving copies.")
    p.add_argument("--version", action="version", version=f"lastcopy {__version__}")
    p.add_argument("--db", default="lastcopy.db", help="SQLite store path")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("ingest", help="load a candidate-lot CSV (ISBN or bib rows)")
    sp.add_argument("--csv", required=True)
    sp.add_argument("--bib-mode", action="store_true",
                    help="key rows on title|author|year (pre-ISBN bib stubs, D1)")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("enrich", help="check sources for digital surrogates")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--source", default=None,
                    help="comma list: ol,ia,wd,gb (gb needs a Google Books key; "
                         "default = ol,ia,wd + gb when a key resolves); ht,loc are M3+ stubs")
    sp.add_argument("--retry-unavailable", action="store_true",
                    help="re-enqueue every source_hits row with status=unavailable "
                         "before enriching (SPEC M3.3 backfill; idempotent)")
    sp.set_defaults(fn=cmd_enrich)

    sp = sub.add_parser("classify", help="apply the classification matrix")
    sp.set_defaults(fn=cmd_classify)

    sp = sub.add_parser("report", help="RED list + counts + roll-up")
    sp.add_argument("--md")
    sp.add_argument("--csv")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("survey", help="random OL sample -> %% no-surrogate + Wilson CI (M3.2)")
    sp.add_argument("--sample", type=int, default=5000)
    sp.add_argument("--from", dest="from_year", type=int, default=1900)
    sp.add_argument("--to", dest="to_year", type=int, default=1980)
    sp.add_argument("--source", default=None,
                    help="survey sources: ia,wd,gb (default ia,gb when a gb key resolves)")
    sp.add_argument("--filter", dest="survey_filter", default=None,
                    help="appended to the OL query, e.g. 'language:por'")
    sp.add_argument("--rows",
                    help="write the citable per-row dataset CSV to this path")
    sp.add_argument("--md")
    sp.set_defaults(fn=cmd_survey)
    sp = sub.add_parser("confirm", help="manual OCLC/BookFinder annotations in (M3)")
    sp.set_defaults(fn=cmd_confirm)

    sp = sub.add_parser("ingest-works",
                        help="stored OL works+authors dumps -> works_ref/authors_ref (M4)")
    sp.add_argument("--data-dir", default="data",
                    help="dir holding ol_dump_{works,authors}_*.txt.gz "
                         "(the data/ symlink to the HC volume)")
    sp.add_argument("--works", help="explicit works dump path (overrides --data-dir)")
    sp.add_argument("--authors", help="explicit authors dump path (overrides --data-dir)")
    sp.set_defaults(fn=cmd_ingest_works)

    sp = sub.add_parser("ingest-editions",
                        help="stream the editions dump (curl|gunzip|parse) -> editions_ref (M4)")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--stream-url", default=None,
                     help="gz dump URL to stream (never written to disk)")
    src.add_argument("--file", default=None,
                     help="local gz dump path (fixtures / offline replay)")
    sp.add_argument("--retries", type=int, default=3,
                    help="stream restarts from zero on mid-stream failure")
    sp.set_defaults(fn=cmd_ingest_editions)

    sp = sub.add_parser("gen-candidates",
                        help="IA-empty x edition_count<=max join + extinction-prior score (M4)")
    sp.add_argument("--max-editions", type=int, default=1)
    sp.add_argument("--lang", default=None, help="exact language code filter, e.g. por")
    sp.add_argument("--from", dest="from_year", type=int, default=None)
    sp.add_argument("--to", dest="to_year", type=int, default=None)
    sp.set_defaults(fn=cmd_gen_candidates)

    sp = sub.add_parser("export-list",
                        help="THE LIST: top-N candidates as CC0 CSV/MD (M4)")
    sp.add_argument("--top", type=int, default=1000)
    sp.add_argument("--csv", default=None, help="output CSV (feeds ingest --csv)")
    sp.add_argument("--md", default=None, help="output Markdown table")
    sp.add_argument("--editions-dump", default=None,
                    help="local gz editions dump path; one streaming pass "
                         "backfills winners' titles (slim editions_ref stores none)")
    sp.add_argument("--workset", action="store_true",
                    help="export the M5 enrich_workset WITH status columns, "
                         "ordered by severity (CR,EN,VU,NT,DD) then score DESC")
    sp.set_defaults(fn=cmd_export_list)

    sp = sub.add_parser("backfill-oclc",
                        help="stage 0: build enrich_workset (top-N candidates) "
                             "+ stream editions dump once -> OCLC backfill (M5)")
    sp.add_argument("--file", required=True, help="local gz editions dump path")
    sp.add_argument("--limit", type=int, default=50_000,
                    help="workset size (top-N by score DESC, isbn13 ASC)")
    sp.set_defaults(fn=cmd_backfill_oclc)

    sp = sub.add_parser("enrich-ht",
                        help="stage 1: hathifiles TSV two-pass join -> ht_access (zero API, M5)")
    sp.add_argument("--hathifile", required=True, help="hathifiles TSV path")
    sp.set_defaults(fn=cmd_enrich_ht)

    sp = sub.add_parser("enrich-wikidata",
                        help="stage 2: offline parse of saved SPARQL JSON -> wd_fulltext (M5)")
    sp.add_argument("--results", required=True,
                    help="saved SPARQL JSON result file (fetched once by the runner)")
    sp.set_defaults(fn=cmd_enrich_wikidata)

    sp = sub.add_parser("enrich-ia",
                        help="stage 3: batched IA advancedsearch plan / executor (the only "
                             "API phase; explicit --execute required for network, M5)")
    sp.add_argument("--batch", type=int, default=30,
                    help="keys per advancedsearch OR-query (plan mode)")
    sp.add_argument("--arraysize", type=int, default=1,
                    help="array-job chunk size hint for the slurm runner (not used here)")
    sp.add_argument("--execute", nargs="+", type=int, default=None,
                    metavar="IDX",
                    help="execute mode: run these ia_plan element index(es) "
                         "with rate discipline; reruns skip done rows")
    sp.add_argument("--results-dir", default=None, metavar="DIR",
                    help="execute mode (M5.8): write per-element "
                         "slice_<idx>.jsonl + done_<idx> markers under DIR "
                         "instead of DB writes (DB opened read-only; "
                         "consolidate later via merge-ia-results)")
    sp.set_defaults(fn=cmd_enrich_ia)

    sp = sub.add_parser(
        "merge-ia-results",
        help="M5.8: single-writer consolidation of parallel-executor "
             "slice_*.jsonl files into enrich_status "
             "(applied/skipped/unknown counts; idempotent)")
    sp.add_argument("--results-dir", required=True, metavar="DIR",
                    help="dir holding slice_*.jsonl + done_<idx> markers "
                         "from enrich-ia --execute --results-dir")
    sp.set_defaults(fn=cmd_merge_ia_results)

    sp = sub.add_parser("assign-status",
                        help="stage 4: Book Red List rules CR/EN/VU/NT/DD + "
                             "status_basis + chained custody tag "
                             "(pure rules, no network, M5)")
    sp.set_defaults(fn=cmd_assign_status)

    sp = sub.add_parser("assign-custody",
                        help="stage 4b (M5.6): custody-quality tag "
                             "open/restricted/none/unknown from existing "
                             "evidence columns (idempotent, no network)")
    sp.set_defaults(fn=cmd_assign_custody)

    sp = sub.add_parser("ocaid-sweep",
                        help="stage 3b (M5.5): stream the editions dump once, "
                             "stage cross-edition work ia/ocaid links, upgrade "
                             "workset ia_identifier (ia_source='ocaid')")
    sp.add_argument("--file", required=True,
                    help="local gz editions dump path (same fixtures as ingest-editions)")
    sp.add_argument("--max-records", type=int, default=None,
                    help="cap the stream after N records (canary runs)")
    sp.set_defaults(fn=cmd_ocaid_sweep)

    sp = sub.add_parser("gb-trickle",
                        help="stage 3c (M5.5): budgeted Google Books "
                             "verification -> gb_status/gb_identifier "
                             "(CR-first, ~1 req/s, resumable)")
    sp.add_argument("--budget", type=int, required=True,
                    help="max requests this run (scrontab: 1000/day)")
    sp.add_argument("--key-file", default=None,
                    help="Google Books key file (default: "
                         "/root/projects/lastcopy/secrets/gbooks.key, "
                         "then ~/.config/lastcopy/gbooks.key)")
    sp.set_defaults(fn=cmd_gb_trickle)

    sp = sub.add_parser(
        "fetch-holdings-bulk",
        help="stage A (M5.7): download bulk national-library MARC21-xml "
             "sets (dnb full copy / loc BooksAll.2016 / ndl weekly ZIPs) "
             "under data/holdings/<inst>/ — resumable curl -C -, "
             "gzip-integrity-checked, download only (no parsing)")
    sp.add_argument("--institution", required=True,
                    choices=["dnb", "loc", "ndl"],
                    help="bulk source (bnf is SRU-first, bl blocked)")
    sp.add_argument("--data-dir", default="data/holdings",
                    help="root dir for bulk files")
    sp.add_argument("--parts", nargs="+", type=int, default=None,
                    metavar="N",
                    help="part numbers (dnb 1-5, loc 1-43; default all)")
    sp.add_argument("--weeks", nargs="+", default=None, metavar="YYYYWW",
                    help="NDL ISO week numbers, e.g. 202637 (required for ndl)")
    sp.set_defaults(fn=cmd_fetch_holdings_bulk)

    sp = sub.add_parser(
        "parse-holdings-bulk",
        help="stage A (M5.7): stream MARC21-xml (.xml/.xml.gz/.zip), "
             "extract 020 $a ISBNs, upsert workset-matched holdings rows "
             "(RAM-bounded, idempotent)")
    sp.add_argument("--institution", required=True,
                    choices=["dnb", "loc", "ndl", "bnf"],
                    help="institution code tagged on the holdings rows")
    sp.add_argument("--file", required=True, nargs="+",
                    help="bulk file path(s) from fetch-holdings-bulk")
    sp.set_defaults(fn=cmd_parse_holdings_bulk)

    sp = sub.add_parser(
        "enrich-holdings",
        help="stage B (M5.7): SRU top-up per institution (dnb/ndl/bnf/loc), "
             "workset ISBNs with no holdings row yet; <=2 rps, IPv4-forced, "
             "budget-capped, resumable; loc uses plain http://lx2:210 (TLS "
             "broken on that port)")
    sp.add_argument("--institution", required=True,
                    choices=["dnb", "ndl", "bnf", "loc"])
    sp.add_argument("--budget", type=int, default=1000,
                    help="max SRU requests this run")
    sp.set_defaults(fn=cmd_enrich_holdings)

    sp = sub.add_parser(
        "assign-holdings-summary",
        help="M5.7: derive enrich_status.holdings (comma-joined institution "
             "codes) from the holdings table (status/custody untouched)")
    sp.set_defaults(fn=cmd_assign_holdings_summary)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "fn"):
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
