import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    ip TEXT NOT NULL,
    owner_token_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | sent
    text TEXT NOT NULL DEFAULT '',
    from_name TEXT NOT NULL DEFAULT '',
    burn_after_download INTEGER NOT NULL DEFAULT 0,
    -- The code was handed out (QR, or a link in a delivered message): the expiry is the sender's.
    code_shared INTEGER NOT NULL DEFAULT 0,
    -- QR uploads may also ask for a password (services/passwords.py); NULL when they don't.
    password_hash TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uploads_ip ON uploads (ip);
CREATE INDEX IF NOT EXISTS ix_uploads_expires_at ON uploads (expires_at);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    upload_id TEXT NOT NULL REFERENCES uploads (id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    downloaded_at REAL
);
CREATE INDEX IF NOT EXISTS ix_files_upload_id ON files (upload_id);

-- No foreign key: deliveries outlive cancelled uploads so they keep counting towards the send limit.
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    upload_id TEXT NOT NULL,
    channel TEXT NOT NULL,  -- email | telegram
    recipient TEXT NOT NULL,
    ip TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    error TEXT,
    compress_media INTEGER NOT NULL DEFAULT 1,  -- Telegram: photos and videos shown in the chat
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_upload_id ON deliveries (upload_id);
CREATE INDEX IF NOT EXISTS ix_deliveries_ip ON deliveries (ip, channel, created_at);

-- Burn-after-download uploads the recipient took: outlives the upload (deleted right after),
-- so the sender's history can still show it was downloaded or read.
CREATE TABLE IF NOT EXISTS taken_uploads (
    upload_id TEXT PRIMARY KEY,
    owner_token_hash TEXT NOT NULL,
    taken_at REAL NOT NULL
);

-- People the Telegram account has written to: greeted once, and whether they sent /stop.
CREATE TABLE IF NOT EXISTS telegram_recipients (
    user_id INTEGER PRIMARY KEY,
    greeted_at REAL,
    stopped_at REAL
);

-- What happened, for the stats site; outlives the uploads (services/events.py).
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    visitor TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS failed_attempts (
    ip TEXT NOT NULL,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_failed_attempts_ip ON failed_attempts (ip, timestamp);
"""


SCHEMA_VERSION = 6

# Steps from each schema version to the next one.
MIGRATIONS = {
    2: """
    ALTER TABLE uploads ADD COLUMN burn_after_download INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE files ADD COLUMN downloaded_at REAL;
    """,
    3: """
    ALTER TABLE uploads ADD COLUMN code_shared INTEGER NOT NULL DEFAULT 0;
    UPDATE uploads SET code_shared = 1 WHERE status = 'sent';
    """,
    4: lambda conn: _add_column(conn, "deliveries", "compress_media INTEGER NOT NULL DEFAULT 1"),
    5: lambda conn: _add_column(conn, "uploads", "password_hash TEXT"),
}


def _add_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """Adds a column to a table that exists; a missing table is created with it by SCHEMA."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def init_db(path: Path) -> None:
    with connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'uploads'").fetchone()
        if has_tables:
            while version in MIGRATIONS:
                step = MIGRATIONS[version]
                if callable(step):
                    step(conn)
                else:
                    conn.executescript(step)
                version += 1
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"Database {path} has an outdated schema; delete it to recreate")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Opens a connection; the transaction commits on success and rolls back on error."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()
