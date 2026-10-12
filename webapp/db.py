"""SQLite access layer for the Clay Table Builder web app."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = Path(os.environ.get("CLAYBUILDER_DB") or (DATA_DIR / "app.db"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  folder_id TEXT NOT NULL DEFAULT '',
  defaults_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id INTEGER NOT NULL REFERENCES clients(id),
  name TEXT NOT NULL,
  filename TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'review'
);

CREATE TABLE IF NOT EXISTS audiences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  client_id INTEGER NOT NULL REFERENCES clients(id),
  audience_id TEXT NOT NULL,
  audience_name TEXT NOT NULL,
  query TEXT NOT NULL,
  edited_query TEXT,
  target_people INTEGER,
  priority INTEGER,
  tier TEXT NOT NULL DEFAULT '',
  dedupe_scope TEXT NOT NULL DEFAULT 'client',
  max_contacts_per_company INTEGER,
  status TEXT NOT NULL DEFAULT 'new',
  lint_json TEXT,
  api_json TEXT,
  preview_count INTEGER,
  repaired_query TEXT,
  repair_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  audience_row_id INTEGER NOT NULL REFERENCES audiences(id),
  client_id INTEGER NOT NULL REFERENCES clients(id),
  status TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  run_dir TEXT,
  work_dir TEXT,
  log_path TEXT,
  error TEXT,
  progress_json TEXT,
  resume_of INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audiences_batch ON audiences(batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(target, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(path: Path | str | None = None) -> None:
    con = connect(path)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def kv_get(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    con.commit()


def kv_json(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    raw = kv_get(con, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
