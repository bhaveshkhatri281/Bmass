"""Database connection, schema creation, and query helpers for SQLite."""

from __future__ import annotations

import datetime
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "mailer.db"

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS inboxes (
  id               INTEGER PRIMARY KEY,
  email            TEXT NOT NULL UNIQUE,
  display_name     TEXT NOT NULL,
  sender_title     TEXT DEFAULT '',
  role             TEXT NOT NULL DEFAULT 'send' CHECK (role IN ('send','monitor')),
  active           INTEGER NOT NULL DEFAULT 1,
  paused_reason    TEXT,
  paused_until     TEXT,
  ramp_start_date  TEXT NOT NULL,
  ramp_start_cap   INTEGER NOT NULL DEFAULT 10,
  ramp_increment   INTEGER NOT NULL DEFAULT 5,
  ramp_every_days  INTEGER NOT NULL DEFAULT 3,
  max_cap          INTEGER NOT NULL DEFAULT 80,
  next_send_after  TEXT,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
  id              INTEGER PRIMARY KEY,
  filename        TEXT NOT NULL,
  sheet           TEXT,
  imported_count  INTEGER NOT NULL,
  skipped_json    TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  id               INTEGER PRIMARY KEY,
  email            TEXT NOT NULL UNIQUE,
  business_name    TEXT NOT NULL DEFAULT '',
  category         TEXT DEFAULT '',
  country          TEXT DEFAULT '',
  location         TEXT DEFAULT '',
  website          TEXT DEFAULT '',
  instagram        TEXT DEFAULT '',
  email_type       TEXT DEFAULT '',
  priority         INTEGER DEFAULT 5,
  quality          TEXT DEFAULT '',
  language         TEXT NOT NULL DEFAULT 'en',
  custom_line      TEXT DEFAULT '',
  inbox_id         INTEGER REFERENCES inboxes(id),
  step             INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','in_sequence','replied','unsubscribed','bounced','finished','skipped')),
  thread_id        TEXT,
  first_subject    TEXT,
  last_message_id  TEXT,
  last_sent_at     TEXT,
  next_due_at      TEXT,
  import_id        INTEGER REFERENCES imports(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, next_due_at);
CREATE INDEX IF NOT EXISTS idx_leads_thread ON leads(thread_id);

CREATE TABLE IF NOT EXISTS steps (
  step_no     INTEGER PRIMARY KEY,
  delay_days  INTEGER NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS languages (
  code         TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  footer_text  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
  step_no   INTEGER NOT NULL REFERENCES steps(step_no),
  language  TEXT NOT NULL REFERENCES languages(code),
  subject   TEXT NOT NULL DEFAULT '',
  body      TEXT NOT NULL,
  PRIMARY KEY (step_no, language)
);

CREATE TABLE IF NOT EXISTS country_settings (
  country   TEXT PRIMARY KEY,
  language  TEXT NOT NULL REFERENCES languages(code),
  timezone  TEXT NOT NULL,
  skip      INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS category_phrases (
  raw        TEXT PRIMARY KEY,
  phrase_en  TEXT NOT NULL,
  phrase_fr  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
  email     TEXT PRIMARY KEY,
  reason    TEXT NOT NULL CHECK (reason IN ('unsubscribed','bounced','manual')),
  added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS send_log (
  id               INTEGER PRIMARY KEY,
  ts               TEXT NOT NULL,
  inbox_id         INTEGER REFERENCES inboxes(id),
  lead_id          INTEGER REFERENCES leads(id),
  lead_email       TEXT NOT NULL,
  step_no          INTEGER NOT NULL,
  subject          TEXT,
  gmail_message_id TEXT,
  thread_id        TEXT,
  result           TEXT NOT NULL CHECK (result IN ('sent','dry_run','error','test')),
  error_text       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_send_once ON send_log(lead_id, step_no) WHERE result = 'sent';

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  inbox_id    INTEGER REFERENCES inboxes(id),
  lead_id     INTEGER REFERENCES leads(id),
  email       TEXT,
  type        TEXT NOT NULL CHECK (type IN ('reply','unsubscribe','bounce','auto_reply_ignored')),
  gmail_message_id TEXT UNIQUE,
  subject     TEXT,
  snippet     TEXT,
  handled     INTEGER NOT NULL DEFAULT 0
);
"""


def utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 string format with Z suffix."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create a new SQLite connection with foreign keys and WAL mode configured."""
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional scope around a series of operations."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path: Path | str | None = None) -> None:
    """Initialize SQLite database tables and indexes."""
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(target_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


# ==============================================================================
# Typed Query Helpers
# ==============================================================================

def get_app_state(key: str, default: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Read a value from app_state table."""
    def _run(c: sqlite3.Connection) -> Optional[str]:
        cur = c.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return str(row["value"]) if row and row["value"] is not None else default

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def set_app_state(key: str, value: Optional[str], conn: Optional[sqlite3.Connection] = None) -> None:
    """Write or update a key in app_state table."""
    def _run(c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        c.commit()

    if conn is not None:
        _run(conn)
    else:
        with get_conn() as c:
            _run(c)


def get_inbox_by_id(inbox_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[sqlite3.Row]:
    """Fetch an inbox by ID."""
    def _run(c: sqlite3.Connection) -> Optional[sqlite3.Row]:
        cur = c.execute("SELECT * FROM inboxes WHERE id = ?", (inbox_id,))
        return cur.fetchone()

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def get_inbox_by_email(email: str, conn: Optional[sqlite3.Connection] = None) -> Optional[sqlite3.Row]:
    """Fetch an inbox by email address."""
    def _run(c: sqlite3.Connection) -> Optional[sqlite3.Row]:
        cur = c.execute("SELECT * FROM inboxes WHERE lower(email) = lower(?)", (email.strip(),))
        return cur.fetchone()

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def list_inboxes(conn: Optional[sqlite3.Connection] = None) -> List[sqlite3.Row]:
    """List all configured inboxes."""
    def _run(c: sqlite3.Connection) -> List[sqlite3.Row]:
        cur = c.execute("SELECT * FROM inboxes ORDER BY id ASC")
        return cur.fetchall()

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def get_active_steps(conn: Optional[sqlite3.Connection] = None) -> List[sqlite3.Row]:
    """List all active steps sorted by step_no."""
    def _run(c: sqlite3.Connection) -> List[sqlite3.Row]:
        cur = c.execute("SELECT * FROM steps WHERE active = 1 ORDER BY step_no ASC")
        return cur.fetchall()

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def get_template(step_no: int, language: str, conn: Optional[sqlite3.Connection] = None) -> Optional[sqlite3.Row]:
    """Fetch a template for a given step and language."""
    def _run(c: sqlite3.Connection) -> Optional[sqlite3.Row]:
        cur = c.execute("SELECT * FROM templates WHERE step_no = ? AND language = ?", (step_no, language))
        return cur.fetchone()

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def is_suppressed(email: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Check if an email address is in the suppression list."""
    def _run(c: sqlite3.Connection) -> bool:
        cur = c.execute("SELECT 1 FROM suppression WHERE lower(email) = lower(?)", (email.strip(),))
        return cur.fetchone() is not None

    if conn is not None:
        return _run(conn)
    with get_conn() as c:
        return _run(c)


def add_suppression(email: str, reason: str, conn: Optional[sqlite3.Connection] = None) -> None:
    """Add an email to the suppression table."""
    now = utc_now_iso()
    def _run(c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO suppression (email, reason, added_at) VALUES (lower(?), ?, ?) ON CONFLICT(email) DO UPDATE SET reason = excluded.reason",
            (email.strip(), reason, now),
        )
        c.commit()

    if conn is not None:
        _run(conn)
    else:
        with get_conn() as c:
            _run(c)

