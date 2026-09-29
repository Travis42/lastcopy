"""SQLite store: spec schema + crash-safe resumable queue + cache + rate-limit ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from datetime import datetime, timezone


from . import CACHE_TTL_DAYS
from .models import (
    Classification,
    Edition,
    HitStatus,
    Rarity,
    SourceHit,
    SourceName,
    Surrogate,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS editions (
  work_key TEXT PRIMARY KEY,
  isbn13 TEXT, isbn10 TEXT, title TEXT, author TEXT, year INTEGER,
  publisher TEXT, imprint_place TEXT, language TEXT, origin_note TEXT,
  edition_count INTEGER
);
CREATE TABLE IF NOT EXISTS source_hits (
  work_key TEXT, source TEXT, status TEXT, checked_at TEXT, evidence_json TEXT,
  PRIMARY KEY (work_key, source)
);
CREATE TABLE IF NOT EXISTS surrogates (
  work_key TEXT, provider TEXT, access TEXT, identifier TEXT, url TEXT,
  PRIMARY KEY (work_key, provider, identifier)
);
CREATE TABLE IF NOT EXISTS rarity (
  work_key TEXT PRIMARY KEY, oclc_holdings INTEGER, for_sale_count INTEGER,
  method TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS classification (
  work_key TEXT PRIMARY KEY, cls TEXT, rationale TEXT, computed_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command TEXT, started_at TEXT, finished_at TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS ratelimit_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  host TEXT, ts REAL, url TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS idx_ratelimit_host ON ratelimit_ledger(host, id);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY, url TEXT, params_json TEXT, status INTEGER,
  body TEXT, stored_at REAL
);
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_key TEXT, source TEXT, status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, last_error TEXT, updated_at REAL,
  UNIQUE (work_key, source)
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
"""


class Store:
    def __init__(self, path: str | Path = "lastcopy.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # crash-safe
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Nullable-column additions, safe on existing DBs (SPEC M3.2): pragma
        column-check + ALTER TABLE; CREATE TABLE only covers fresh DBs."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(editions)")}
        if "edition_count" not in cols:
            self.conn.execute("ALTER TABLE editions ADD COLUMN edition_count INTEGER")
        if "language" not in cols:
            self.conn.execute("ALTER TABLE editions ADD COLUMN language TEXT")

    def close(self) -> None:
        self.conn.close()

    # -- runs ------------------------------------------------------------
    def start_run(self, command: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (command, started_at) VALUES (?, ?)",
            (command, _now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, details: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, details = ? WHERE id = ?",
            (_now_iso(), json.dumps(details or {}), run_id),
        )
        self.conn.commit()

    # -- editions ----------------------------------------------------------
    def upsert_edition(self, ed: Edition) -> None:
        self.conn.execute(
            """INSERT INTO editions (work_key, isbn13, isbn10, title, author, year,
                                     publisher, imprint_place, language, origin_note,
                                     edition_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(work_key) DO UPDATE SET
                  isbn13=excluded.isbn13, isbn10=excluded.isbn10, title=excluded.title,
                  author=excluded.author, year=excluded.year, publisher=excluded.publisher,
                  imprint_place=excluded.imprint_place, language=excluded.language,
                  origin_note=excluded.origin_note, edition_count=excluded.edition_count""",
            (ed.work_key, ed.isbn13, ed.isbn10, ed.title, ed.author, ed.year,
             ed.publisher, ed.imprint_place, ed.language, ed.origin_note,
             ed.edition_count),
        )
        self.conn.commit()

    def all_editions(self) -> list[Edition]:
        rows = self.conn.execute("SELECT * FROM editions ORDER BY work_key").fetchall()
        return [Edition(**dict(r)) for r in rows]

    # -- queue (crash-safe resumable) ---------------------------------------
    def enqueue(self, work_key: str, source: str) -> None:
        self.conn.execute(
            """INSERT INTO queue (work_key, source, status, attempts, updated_at)
               VALUES (?,?, 'pending', 0, ?)
               ON CONFLICT(work_key, source) DO UPDATE SET status='pending'
                 WHERE queue.status != 'done'""",
            (work_key, source, time.time()),
        )
        self.conn.commit()

    def pending(self, source: str | None = None) -> list[sqlite3.Row]:
        if source:
            return self.conn.execute(
                "SELECT * FROM queue WHERE status='pending' AND source=? ORDER BY id",
                (source,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM queue WHERE status='pending' ORDER BY id"
        ).fetchall()

    def queue_mark(self, work_key: str, source: str, status: str,
                   error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE queue SET status=?, attempts=attempts+1, last_error=?, updated_at=?
               WHERE work_key=? AND source=?""",
            (status, error, time.time(), work_key, source),
        )
        self.conn.commit()

    # -- source hits / surrogates / rarity / classification -------------------
    def save_hit(self, hit: SourceHit) -> None:
        self.conn.execute(
            """INSERT INTO source_hits (work_key, source, status, checked_at, evidence_json)
               VALUES (?,?,?,?,?)
               ON CONFLICT(work_key, source) DO UPDATE SET
                 status=excluded.status, checked_at=excluded.checked_at,
                 evidence_json=excluded.evidence_json""",
            (hit.work_key, hit.source.value, hit.status.value,
             hit.checked_at.isoformat(), json.dumps(hit.evidence_json)),
        )
        self.conn.commit()

    def save_surrogates(self, work_key: str, surrogates: list[Surrogate]) -> None:
        for s in surrogates:
            self.conn.execute(
                """INSERT INTO surrogates (work_key, provider, access, identifier, url)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(work_key, provider, identifier) DO UPDATE SET
                     access=excluded.access, url=excluded.url""",
                (work_key, s.provider, s.access.value, s.identifier, s.url),
            )
        self.conn.commit()

    def surrogates_for(self, work_key: str) -> list[Surrogate]:
        rows = self.conn.execute(
            "SELECT * FROM surrogates WHERE work_key=?", (work_key,)
        ).fetchall()
        return [Surrogate(**dict(r)) for r in rows]

    def save_rarity(self, r: Rarity) -> None:
        self.conn.execute(
            """INSERT INTO rarity (work_key, oclc_holdings, for_sale_count, method, checked_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(work_key) DO UPDATE SET oclc_holdings=excluded.oclc_holdings,
                 for_sale_count=excluded.for_sale_count, method=excluded.method,
                 checked_at=excluded.checked_at""",
            (r.work_key, r.oclc_holdings, r.for_sale_count, r.method,
             r.checked_at.isoformat()),
        )
        self.conn.commit()

    def rarity_for(self, work_key: str) -> Rarity | None:
        row = self.conn.execute(
            "SELECT * FROM rarity WHERE work_key=?", (work_key,)
        ).fetchone()
        return Rarity(**dict(row)) if row else None

    def save_classification(self, c: Classification) -> None:
        self.conn.execute(
            """INSERT INTO classification (work_key, cls, rationale, computed_at)
               VALUES (?,?,?,?)
               ON CONFLICT(work_key) DO UPDATE SET cls=excluded.cls,
                 rationale=excluded.rationale, computed_at=excluded.computed_at""",
            (c.work_key, c.cls.value, json.dumps(c.rationale), c.computed_at.isoformat()),
        )
        self.conn.commit()

    def all_classifications(self) -> list[Classification]:
        rows = self.conn.execute("SELECT * FROM classification").fetchall()
        return [
            Classification(
                work_key=r["work_key"],
                cls=r["cls"],
                rationale=json.loads(r["rationale"]),
                computed_at=r["computed_at"],
            )
            for r in rows
        ]

    # -- rate-limit ledger -----------------------------------------------------
    def last_request_ts(self, host: str) -> float | None:
        row = self.conn.execute(
            "SELECT ts FROM ratelimit_ledger WHERE host=? ORDER BY id DESC LIMIT 1",
            (host,),
        ).fetchone()
        return row["ts"] if row else None

    def log_request(self, host: str, url: str, status: str) -> None:
        self.conn.execute(
            "INSERT INTO ratelimit_ledger (host, ts, url, status) VALUES (?,?,?,?)",
            (host, time.time(), url, status),
        )
        self.conn.commit()

    def ledger_for_host(self, host: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM ratelimit_ledger WHERE host=? ORDER BY id", (host,)
        ).fetchall()

    # -- cache (TTL 30d, keyed on url+params) -----------------------------------
    @staticmethod
    def cache_key(url: str, params: dict | None) -> str:
        blob = url + "?" + json.dumps(params or {}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def cache_get(self, url: str, params: dict | None) -> tuple[int, str] | None:
        key = self.cache_key(url, params)
        row = self.conn.execute(
            "SELECT status, body, stored_at FROM cache WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        if time.time() - row["stored_at"] > CACHE_TTL_DAYS * 86400:
            return None  # expired
        return (row["status"], row["body"])

    def cache_put(self, url: str, params: dict | None, status: int, body: str) -> None:
        key = self.cache_key(url, params)
        stored = dict(params or {})
        if "key" in stored:  # never persist API keys in the DB (googleapis ?key=)
            stored["key"] = "<redacted>"
        self.conn.execute(
            """INSERT INTO cache (key, url, params_json, status, body, stored_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET status=excluded.status,
                 body=excluded.body, stored_at=excluded.stored_at""",
            (key, url, json.dumps(stored, sort_keys=True), status, body, time.time()),
        )
        self.conn.commit()

    def clear_cache(self) -> None:
        self.conn.execute("DELETE FROM cache")
        self.conn.commit()


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
