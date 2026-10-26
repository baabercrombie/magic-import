"""Small SQLite layer. Everything the app does is recorded here."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- email | upload | text
    message_id TEXT,
    from_addr TEXT,
    subject TEXT,
    received_at TEXT,
    processed_at TEXT,
    status TEXT NOT NULL,              -- processing | done | error | ignored
    error TEXT,
    ai_summary TEXT,
    body_text TEXT,
    attachments TEXT,                  -- JSON list of file names
    raw_path TEXT                      -- saved copy of the original email/file
);
CREATE INDEX IF NOT EXISTS idx_imports_message_id ON imports(message_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    title TEXT NOT NULL,
    start TEXT NOT NULL,               -- YYYY-MM-DD (all day) or YYYY-MM-DDTHH:MM (local time)
    end TEXT,                          -- same format; for all day this is the LAST day (inclusive)
    all_day INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    description TEXT,
    status TEXT NOT NULL,              -- pending | created | duplicate | skipped | deleted | failed
    duplicate_of TEXT,
    calendar_url TEXT,
    uid TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_import ON events(import_id);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    import_id INTEGER,
    event_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.FILES_DIR, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


# ---------- settings ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def review_mode() -> bool:
    value = get_setting("review_mode")
    if value is None:
        return config.REVIEW_MODE_DEFAULT
    return value == "1"


# ---------- activity log ----------

def log(action: str, detail: str = "", import_id: int | None = None, event_id: int | None = None) -> None:
    execute(
        "INSERT INTO activity(ts, import_id, event_id, action, detail) VALUES(?, ?, ?, ?, ?)",
        (now_iso(), import_id, event_id, action, detail),
    )


# ---------- imports ----------

def create_import(source: str, message_id: str | None, from_addr: str, subject: str,
                  received_at: str, body_text: str, attachments: list[str],
                  raw_path: str | None, status: str = "processing") -> int:
    return execute(
        "INSERT INTO imports(source, message_id, from_addr, subject, received_at, status, "
        "body_text, attachments, raw_path) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, message_id, from_addr, subject, received_at, status,
         body_text, json.dumps(attachments), raw_path),
    )


def update_import(import_id: int, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE imports SET {keys} WHERE id = ?", (*fields.values(), import_id))


def get_import(import_id: int) -> dict | None:
    row = query_one("SELECT * FROM imports WHERE id = ?", (import_id,))
    if row:
        row["attachments"] = json.loads(row["attachments"] or "[]")
    return row


def message_already_seen(message_id: str) -> bool:
    return query_one("SELECT id FROM imports WHERE message_id = ?", (message_id,)) is not None


# ---------- events ----------

def create_event(import_id: int, ev: dict, status: str) -> int:
    ts = now_iso()
    return execute(
        "INSERT INTO events(import_id, title, start, end, all_day, location, description, status, "
        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (import_id, ev["title"], ev["start"], ev.get("end"), 1 if ev.get("all_day") else 0,
         ev.get("location") or "", ev.get("description") or "", status, ts, ts),
    )


def update_event(event_id: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    keys = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE events SET {keys} WHERE id = ?", (*fields.values(), event_id))


def get_event(event_id: int) -> dict | None:
    return query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def events_for_import(import_id: int) -> list[dict]:
    return query("SELECT * FROM events WHERE import_id = ? ORDER BY start, id", (import_id,))
