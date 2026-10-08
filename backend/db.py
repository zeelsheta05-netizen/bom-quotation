"""SQLite persistence. One connection guarded by a lock; JSON blobs for quotes."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("BOM_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

_lock = threading.RLock()
_conn = sqlite3.connect(DATA_DIR / "bom.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, filename TEXT, source_url TEXT, media_type TEXT,
  file_path TEXT, thumb_path TEXT, status TEXT, error TEXT,
  inputs TEXT, created_at REAL, started_at REAL, finished_at REAL, quote_id TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
  id TEXT PRIMARY KEY, job_id TEXT, data TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, quote_id TEXT, label TEXT, data TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS inventory (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stone_type TEXT, color TEXT, clarity TEXT,
  price_per_ct REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS labor_rates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, piece_type TEXT UNIQUE COLLATE NOCASE,
  simple REAL, medium REAL, complex REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""
with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


def q(sql, args=(), one=False):
    with _lock:
        cur = _conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def kv_get(key, default=None):
    row = q("SELECT v FROM kv WHERE k=?", (key,), one=True)
    return json.loads(row["v"]) if row else default


def kv_set(key, value):
    x("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
      (key, json.dumps(value)))


def now():
    return time.time()
