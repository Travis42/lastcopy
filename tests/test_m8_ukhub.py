"""M8.3 — UK Library Hub Z39.50 harvester tests (SPEC-M8.3-UKHUB).

Fixtures (all derived from the live-checked session 2026-10-09 against
tcp:3.250.189.53:210, yaz-client `format xml` → MODS with COPAC holdings):
  ukhub_sample.xml          — REAL record excerpt (Pride and prejudice,
                              QUB holding, UkMaC code 'qub')
  ukhub_transcript.txt      — fake 3-ISBN batch transcript: hits 3 with
                              codes qub/cam/nls, 0 hits, hits 2 + syntax
                              diagnostic 238 (count-only degrade)
  ukhub_retry_transcript.txt — plain-term fallback response (1 hit, mmu)
harvest/CLI run against these transcripts via an injected runner — no
network, no yaz-client.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from xml.etree import ElementTree

import pytest

from lastcopy import m4, m5, m8_ukhub
from lastcopy.m8_ukhub import main as join_main
from lastcopy.m8_ukhub import main_import

FIX = Path(__file__).parent / "fixtures"
TRANSCRIPT = (FIX / "ukhub_transcript.txt").read_text(encoding="utf-8")
RETRY_TRANSCRIPT = (FIX / "ukhub_retry_transcript.txt").read_text(
    encoding="utf-8")

P = "9780141439518"    # hits 3: qub cam nls (real record in sample.xml)
Q = "9780141439600"    # transport error on PQF -> plain-term retry: mmu
R = "9780142000000"    # hits 2 + diagnostic 238 -> count-only


# ------------------------------------------------------ script generation
def test_build_pqf():
    assert m8_ukhub.build_pqf("9780141439518") == "@attr 1=7 9780141439518"


def test_build_batch_script_structure():
    script = m8_ukhub.build_batch_script(["9780000000002", "9780000000019"],
                                         show_max=8)
    lines = script.splitlines()
    assert lines[0] == "open tcp:3.250.189.53:210"   # persistent session
    assert lines[1] == "format xml"                   # XML presentation
    assert "find @attr 1=7 9780000000002" in lines
    assert "show 1+8" in lines
    assert lines.count("sleep 1") == 1                # pacing between ISBNs
    assert "find @attr 1=7 9780000000019" in lines
    assert lines[-1] == "quit"
    plain = m8_ukhub.build_batch_script(["9780000000002"], plain=True)
    assert "find 9780000000002" in plain              # fallback bare term


# ---------------------------------------------------------------- parsing
def test_parse_transcript_three_searches():
    got = m8_ukhub.parse_transcript(TRANSCRIPT, [P, Q, R])
    assert got[P]["n_hits"] == 3
    assert got[P]["codes"] == {"qub", "cam", "nls"}
    assert not got[P]["diagnostic"]
    assert got[Q]["n_hits"] == 0 and got[Q]["codes"] == set()
    assert got[R]["n_hits"] == 2 and got[R]["diagnostic"]  # degrade signal
    assert got[R]["codes"] == set()


def test_parse_transcript_missing_hits_and_short_segment():
    # Q's find failed mid-session: no hits line for it -> retry candidate
    broken = TRANSCRIPT.replace(
        "Number of hits: 0, setno 1\nrecords returned: 0\n", "")
    got = m8_ukhub.parse_transcript(broken, [P, Q, R])
    assert got[P]["n_hits"] == 3
    assert got[Q]["n_hits"] is None
    assert got[R]["n_hits"] == 2


def test_parse_transcript_transport_and_truncated():
    dead = TRANSCRIPT.replace("Connection accepted by v3 target.",
                              "Connection refused.")
    assert m8_ukhub.parse_transcript(dead, [P])[P]["n_hits"] is None
    # fewer search segments than isbns: the tail isbns get n_hits None
    got = m8_ukhub.parse_transcript(
        "Connection accepted by v3 target.\nSent searchRequest.\n"
        "Number of hits: 1\n", [P, Q])
    assert got[P]["n_hits"] == 1 and got[Q]["n_hits"] is None


# ------------------------------------------------- real-record XML fixture
def test_real_sample_xml_holdings_codes():
    """The REAL captured MODS record parses and yields the QUB UkMaC code
    via the same regex the transcript parser uses."""
    text = (FIX / "ukhub_sample.xml").read_text(encoding="utf-8")
    root = ElementTree.fromstring(text)
    assert root.tag == "{http://www.loc.gov/mods/v3}mods"
    codes = set(m8_ukhub.CODE_RE.findall(text))
    assert codes == {"qub"}
    # COPAC holdings namespace declared on the real record
    assert "http://copac.ac.uk/schemas/holdings/v1" in text


# ---------------------------------------------------------------- harvest
def _runner_factory(calls):
    def runner(script, timeout=None):
        calls.append(script)
        if len(calls) == 1:
            # first chunk: Q's PQF find fails (no hits line)
            return TRANSCRIPT.replace(
                "Number of hits: 0, setno 1\nrecords returned: 0\n", "")
        return RETRY_TRANSCRIPT                      # plain-term retry for Q
    return runner


def test_harvest_store_retry_and_degrade(tmp_path):
    calls: list[str] = []
    stats = m8_ukhub.harvest({P, Q, R}, tmp_path / "uk.db",
                             runner=_runner_factory(calls),
                             rng=random.Random(0))
    assert stats["matched"] == 3                     # P(3) Q(1, retried) R(2)
    assert stats["errors"] == 0
    assert len(calls) == 2                           # chunk + one retry
    assert "find 9780141439600" in calls[1]          # retry is plain-term
    conn = sqlite3.connect(tmp_path / "uk.db")
    conn.row_factory = sqlite3.Row
    rows = {r["isbn13"]: r for r in conn.execute(
        "SELECT * FROM ukhub_matches")}
    assert rows[P]["n_hits"] == 3
    assert rows[P]["institutions"] == "cam nls qub"
    assert rows[P]["status"] == "ok"
    assert rows[Q]["institutions"] == "mmu"          # via fallback retry
    assert rows[R]["n_hits"] == 2
    assert rows[R]["institutions"] == ""             # count-only degrade
    assert rows[R]["status"] == "ok"
    conn.close()


def test_harvest_err_after_retry(tmp_path):
    def runner(script, timeout=None):
        return "Z> Connecting...OK.\nSent initrequest.\nConnection refused.\n"
    stats = m8_ukhub.harvest({P}, tmp_path / "uk.db", runner=runner,
                             rng=random.Random(0))
    assert stats["errors"] == 1 and stats["matched"] == 0
    row = sqlite3.connect(tmp_path / "uk.db").execute(
        "SELECT n_hits, institutions, status FROM ukhub_matches").fetchone()
    assert row == (0, "", "err")


def test_harvest_resume_skips_done(tmp_path):
    calls: list[str] = []
    runner = _runner_factory(calls)
    db = tmp_path / "uk.db"
    m8_ukhub.harvest({P, Q, R}, db, runner=runner, rng=random.Random(0))
    assert len(calls) == 2
    # resume: all three have status='ok' -> no further sessions
    stats = m8_ukhub.harvest({P, Q, R}, db, runner=runner,
                             rng=random.Random(0))
    assert stats["queried"] == 0 and stats["skipped"] == 3
    assert len(calls) == 2


def test_harvest_progress_log(tmp_path, capsys):
    m8_ukhub.harvest({P}, tmp_path / "uk.db", runner=_runner_factory([]),
                     rng=random.Random(0), log_every=1)
    err = capsys.readouterr().err
    assert err.count("[ukhub-join] done=") >= 1
    assert "matched=1" in err


# ------------------------------------------------------------- CLI smoke
def test_ukhub_join_cli_smoke(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws.isbns"
    ws.write_text("# comment\n" + f"{P}\n{Q}\n{R}\n\n", encoding="utf-8")
    out = tmp_path / "uk.db"
    calls: list[str] = []
    monkeypatch.setattr(m8_ukhub, "run_script", _runner_factory(calls))
    assert join_main(["--workset", str(ws), "--out", str(out),
                      "--limit", "3"]) == 0
    err = capsys.readouterr().err
    assert "workset=3" in err
    assert "z3950.libraryhub.jisc.ac.uk" in err       # /etc/hosts hint
    n = sqlite3.connect(out).execute(
        "SELECT COUNT(*) FROM ukhub_matches WHERE status='ok'").fetchone()[0]
    assert n == 3


# ------------------------------------------------------------ lab import
def _matches_db(tmp_path: Path) -> Path:
    db = tmp_path / "ukhub_matches.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE ukhub_matches (
                     isbn13 TEXT PRIMARY KEY, n_hits INTEGER,
                     institutions TEXT, status TEXT)""")
    conn.executemany(
        "INSERT INTO ukhub_matches VALUES (?,?,?,?)",
        [(P, 3, "cam nls qub", "ok"),
         (Q, 1, "mmu", "ok"),
         (R, 2, "", "ok"),                            # count-only degrade
         ("9780999999991", 0, "", "ok"),              # no match -> skipped
         ("9780999999992", 5, "", "err")])            # err -> skipped
    conn.commit()
    conn.close()
    return db


def test_import_matches_detail_and_summary(tmp_path):
    matches = _matches_db(tmp_path)
    lab = m4.connect(tmp_path / "m4.db")
    m5.ensure_schema(lab)
    lab.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (P,))
    lab.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (R,))
    lab.commit()
    stats = m8_ukhub.import_matches(matches, lab)
    assert stats["inserted"] == 3                     # err/0-hit rows skipped
    assert stats["count_only_detail"] == 1            # R degraded
    rows = lab.execute("SELECT isbn13, record_id, detail FROM holdings "
                       "WHERE institution='jisc-uk' ORDER BY isbn13"
                       ).fetchall()
    by = {r["isbn13"]: r for r in rows}
    assert by[P]["detail"] == "cam nls qub"
    assert by[Q]["detail"] == "mmu"
    assert by[R]["detail"] == "n=2"                   # count-only fallback
    assert all(r["record_id"] is None for r in rows)
    # custody re-derived alongside (P single institution row -> single)
    cust = dict(lab.execute("SELECT isbn13, custody_physical FROM "
                            "enrich_status").fetchall())
    assert cust[P] == "single"
    assert m8_ukhub.import_matches(matches, lab)["inserted"] == 0  # idempot.
    lab.close()


def test_ukhub_import_cli_smoke(tmp_path):
    matches = _matches_db(tmp_path)
    lab_path = tmp_path / "m4.db"
    lab = m4.connect(lab_path)
    m5.ensure_schema(lab)
    lab.execute("INSERT INTO enrich_status (isbn13) VALUES (?)", (P,))
    lab.commit()
    lab.close()
    assert main_import(["--matches", str(matches), "--db",
                        str(lab_path)]) == 0
    lab = m4.connect(lab_path)
    assert lab.execute("SELECT COUNT(*) FROM holdings WHERE "
                       "institution='jisc-uk'").fetchone()[0] == 3
    lab.close()
