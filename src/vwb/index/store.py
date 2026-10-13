"""SQLite observation store.

Typed tables hold the dense facts (fast, friendly to SQL). Open kinds go to `observations`.
The `envelope` view shows every row in the spec's single observation envelope:

    {obs_id, media_id, media_rev, interval [f0, f1), region?, kind, value,
     producer, status: fact | reproduced | claim}

Agents explore with read-only SQL, text search and the other index tools.
"""

from __future__ import annotations

import sqlite3
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from vwb.util.jsonio import dumps

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS media (
  media_id TEXT, rev TEXT, path TEXT, fps REAL, width INTEGER, height INTEGER,
  n_frames INTEGER, duration REAL, info TEXT, PRIMARY KEY (media_id, rev));
CREATE TABLE IF NOT EXISTS stages (
  name TEXT PRIMARY KEY, version TEXT, settings TEXT, seconds REAL, done_at TEXT DEFAULT CURRENT_TIMESTAMP, note TEXT);

CREATE TABLE IF NOT EXISTS shots (
  id INTEGER PRIMARY KEY, f0 INTEGER, f1 INTEGER, t0 REAL, t1 REAL,
  kind TEXT, title TEXT, description TEXT, has_face INTEGER DEFAULT 0, n_text INTEGER DEFAULT 0,
  bg TEXT, producer TEXT DEFAULT 'pyscenedetect', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS words (
  i INTEGER PRIMARY KEY, f0 INTEGER, f1 INTEGER, t0 REAL, t1 REAL, text TEXT, norm TEXT, conf REAL,
  agree INTEGER, alt TEXT, chosen_from TEXT, sentence INTEGER,
  producer TEXT DEFAULT 'asr.consensus', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS sentences (
  id INTEGER PRIMARY KEY, f0 INTEGER, f1 INTEGER, t0 REAL, t1 REAL, text TEXT, w0 INTEGER, w1 INTEGER,
  agree REAL, shot INTEGER, producer TEXT DEFAULT 'asr.consensus', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS text_tracks (
  id INTEGER PRIMARY KEY, f0 INTEGER, f1 INTEGER, t0 REAL, t1 REAL, x REAL, y REAL, w REAL, h REAL,
  text TEXT, text2 TEXT, agree INTEGER, conf REAL, cap_h REAL, role TEXT, regulated INTEGER DEFAULT 0,
  shot INTEGER, block INTEGER, n_seen INTEGER, producer TEXT DEFAULT 'ocr.apple-vision', status TEXT DEFAULT 'fact',
  text3 TEXT);
CREATE TABLE IF NOT EXISTS faces (
  f INTEGER, x REAL, y REAL, w REAL, h REAL, conf REAL, track INTEGER,
  producer TEXT DEFAULT 'apple-vision', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS persons (
  f INTEGER, x REAL, y REAL, w REAL, h REAL, conf REAL, producer TEXT DEFAULT 'apple-vision', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS saliency (
  f INTEGER, x REAL, y REAL, w REAL, h REAL, conf REAL, producer TEXT DEFAULT 'apple-vision', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS blocks (
  id INTEGER PRIMARY KEY, shot INTEGER, x REAL, y REAL, w REAL, h REAL, kind TEXT, text TEXT, area REAL,
  producer TEXT DEFAULT 'layout.segment', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS silences (t0 REAL, t1 REAL, producer TEXT DEFAULT 'ffmpeg.silencedetect', status TEXT DEFAULT 'fact');
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY, t0 REAL, t1 REAL, title TEXT, summary TEXT, producer TEXT, status TEXT DEFAULT 'claim');
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY, t0 REAL, t1 REAL, title TEXT, hook TEXT, why TEXT, score REAL, producer TEXT, status TEXT DEFAULT 'claim');
CREATE TABLE IF NOT EXISTS hazards (
  id INTEGER PRIMARY KEY, kind TEXT, f0 INTEGER, f1 INTEGER, t0 REAL, t1 REAL, text TEXT, quote TEXT,
  x REAL, y REAL, w REAL, h REAL, producer TEXT, status TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS marks (
  id INTEGER PRIMARY KEY, kind TEXT, name TEXT, x REAL, y REAL, w REAL, h REAL, f0 INTEGER, f1 INTEGER,
  best_frame INTEGER, best_box TEXT, persistence REAL, producer TEXT, status TEXT DEFAULT 'claim');
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY, producer TEXT, model TEXT, scope TEXT, f0 INTEGER, f1 INTEGER, json TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS observations (
  obs_id INTEGER PRIMARY KEY, media_id TEXT, media_rev TEXT, f0 INTEGER, f1 INTEGER,
  x REAL, y REAL, w REAL, h REAL, kind TEXT, value TEXT, text TEXT, evidence_ref TEXT,
  producer TEXT, tool TEXT, version TEXT, settings TEXT, status TEXT, supersedes INTEGER,
  created TEXT DEFAULT CURRENT_TIMESTAMP);

CREATE INDEX IF NOT EXISTS words_t ON words(t0);
CREATE INDEX IF NOT EXISTS tt_f ON text_tracks(f0, f1);
CREATE INDEX IF NOT EXISTS faces_f ON faces(f);
CREATE INDEX IF NOT EXISTS sal_f ON saliency(f);
CREATE INDEX IF NOT EXISTS obs_kind ON observations(kind, f0);

DROP VIEW IF EXISTS envelope;
CREATE VIEW envelope AS
  SELECT 'shot' AS kind, f0, f1, NULL AS x, NULL AS y, NULL AS w, NULL AS h,
         COALESCE(title, '') || ' | ' || COALESCE(kind, '') AS value, producer, status FROM shots
  UNION ALL SELECT 'asr.sentence', f0, f1, NULL, NULL, NULL, NULL, text, producer, status FROM sentences
  UNION ALL SELECT 'ocr.text', f0, f1, x, y, w, h, text, producer, status FROM text_tracks
  UNION ALL SELECT 'face', f, f + 1, x, y, w, h, CAST(track AS TEXT), producer, status FROM faces
  UNION ALL SELECT 'layout.block', NULL, NULL, x, y, w, h, kind || ': ' || COALESCE(text, ''), producer, status FROM blocks
  UNION ALL SELECT 'hazard.' || kind, f0, f1, x, y, w, h, COALESCE(quote, text), producer, status FROM hazards
  UNION ALL SELECT 'mark.' || kind, f0, f1, x, y, w, h, name, producer, status FROM marks
  UNION ALL SELECT kind, f0, f1, x, y, w, h, COALESCE(text, value), producer, status FROM observations;
"""


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[tuple[str, int]] = set()


class IndexStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        con = self.connect()
        key = (str(self.path.resolve()), os.stat(self.path).st_ino)
        with _SCHEMA_LOCK:                 # one schema pass per file; threads must not race DROP/CREATE VIEW
            if key not in _SCHEMA_READY:
                with con:
                    con.executescript(SCHEMA)
                    cols = {r[1] for r in con.execute("PRAGMA table_info(text_tracks)")}
                    if "text3" not in cols:                    # indexes built before the third reader
                        con.execute("ALTER TABLE text_tracks ADD COLUMN text3 TEXT")
                _SCHEMA_READY.add(key)

    def connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    # ---- writes ----
    def replace_rows(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        """Replace every row of a typed table (one stage owns each table)."""
        rows = list(rows)
        con = self.connect()
        with con:
            con.execute(f"DELETE FROM {table}")
            if rows:
                cols = list(rows[0].keys())
                con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                [tuple(_cell(r.get(c)) for c in cols) for r in rows])
        return len(rows)

    def insert_rows(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        con = self.connect()
        with con:
            con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [tuple(_cell(r.get(c)) for c in cols) for r in rows])
        return len(rows)

    def add_observation(self, *, media_id: str, media_rev: str, kind: str, f0: int | None, f1: int | None,
                        value: Any, text: str = "", region: list[float] | None = None, producer: str,
                        tool: str = "", version: str = "", settings: Any = None, status: str = "claim",
                        evidence_ref: str = "", supersedes: int | None = None) -> int:
        x, y, w, h = (region or [None] * 4)
        con = self.connect()
        with con:
            cur = con.execute(
                "INSERT INTO observations (media_id, media_rev, f0, f1, x, y, w, h, kind, value, text, evidence_ref,"
                " producer, tool, version, settings, status, supersedes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (media_id, media_rev, f0, f1, x, y, w, h, kind, dumps(value, None), text, evidence_ref, producer,
                 tool, version, dumps(settings, None) if settings is not None else None, status, supersedes))
        return int(cur.lastrowid)

    def mark_stage(self, name: str, version: str, settings: Any, seconds: float, note: str = "") -> None:
        con = self.connect()
        with con:
            con.execute("INSERT OR REPLACE INTO stages (name, version, settings, seconds, note) VALUES (?,?,?,?,?)",
                        (name, version, dumps(settings, None), seconds, note))

    def stage_done(self, name: str, version: str, settings: Any) -> bool:
        row = self.one("SELECT version, settings FROM stages WHERE name=?", (name,))
        return bool(row and row["version"] == version and row["settings"] == dumps(settings, None))

    def set_media(self, media_id: str, rev: str, path: str, info: dict[str, Any]) -> None:
        con = self.connect()
        with con:
            con.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?,?,?,?)",
                        (media_id, rev, path, info["fps"], info["width"], info["height"], info["n_frames"],
                         info["duration"], dumps(info, None)))

    # ---- reads ----
    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.connect().execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self.connect().execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_readonly(self, sql: str, limit: int = 500) -> list[dict[str, Any]]:
        """Agent-facing SQL: one statement, read-only connection, row cap."""
        statement = sql.strip().rstrip(";")
        if ";" in statement:
            raise ValueError("Only one SQL statement per call")
        uri = f"file:{self.path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(statement).fetchmany(limit)
            return [dict(r) for r in rows]
        finally:
            con.close()

    def media(self) -> dict[str, Any]:
        row = self.one("SELECT * FROM media LIMIT 1")
        if not row:
            raise RuntimeError("Index has no media yet; run index_build first")
        return row


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return dumps(value, None)
    if hasattr(value, "item"):
        return value.item()
    return value
