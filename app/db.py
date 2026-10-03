"""SQLite storage. One file under DATA_DIR, WAL mode, foreign keys on."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "bankreco.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    session_nonce TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank TEXT NOT NULL,
    acc_no TEXT NOT NULL,
    nickname TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('main','collection')),
    branch TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (bank, acc_no)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    bank_file TEXT, bank_hash TEXT,
    book_file TEXT, book_hash TEXT,
    bank_open REAL, bank_close REAL,
    book_open REAL, book_close REAL,
    opening_diff_added REAL NOT NULL DEFAULT 0,
    ack_reason TEXT DEFAULT '',
    checks_json TEXT, flags_json TEXT, brs_json TEXT, summary_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_acc ON runs(account_id, period_to);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_period ON runs(account_id, period_from);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_bank_hash ON runs(bank_hash);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_book_hash ON runs(book_hash);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    method TEXT NOT NULL,
    confidence TEXT NOT NULL,
    note TEXT DEFAULT '',
    manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    side TEXT NOT NULL CHECK (side IN ('bank','book')),
    dir TEXT NOT NULL CHECK (dir IN ('in','out')),
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    name TEXT DEFAULT '',
    ref TEXT DEFAULT '',
    data_json TEXT NOT NULL,
    match_id INTEGER REFERENCES matches(id),
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_items_open ON items(account_id, match_id);
CREATE INDEX IF NOT EXISTS ix_items_run ON items(run_id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS holidays (
    day TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS revoked_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    revoked_at TEXT NOT NULL,
    revoked_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    batch TEXT NOT NULL,
    snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    remark TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_subject ON reviews(account_id, subject, id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def dumps(o) -> str:
    return json.dumps(o, default=_default, ensure_ascii=False)


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, detect_types=0, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


MIGRATIONS = [
    # (table, column, definition)
    ("users", "role", "TEXT NOT NULL DEFAULT 'admin'"),      # users created before roles existed were the owner
    ("users", "full_name", "TEXT NOT NULL DEFAULT ''"),
    ("users", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "must_change_pw", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_login", "TEXT DEFAULT ''"),
    ("runs", "checked_by", "TEXT DEFAULT ''"),
    ("runs", "checked_at", "TEXT DEFAULT ''"),
    ("runs", "checked_note", "TEXT DEFAULT ''"),
]


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.close()


def audit(conn, user: str, action: str, detail: str = "") -> None:
    conn.execute("INSERT INTO audit (ts, username, action, detail) VALUES (?,?,?,?)",
                 (now(), user, action, detail))


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
