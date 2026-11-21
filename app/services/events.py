"""An append-only log for the stats site (st.warpflow.ru).

Uploads are deleted once they expire; the log keeps what happened (sizes, channels, times)
for `Settings.events_ttl`. It holds no IPs, names or texts: a visitor is the IP hashed with the
day, enough to count people per day but not to follow anyone across days.
"""

import hashlib
import sqlite3
import time
from datetime import UTC, datetime

DRAFT = "draft"  # someone started a send (opened a draft)
FILE_UPLOADED = "file_uploaded"  # size: the file's bytes
SENT = "sent"  # channel; detail: "password", "burn" flags of a QR code
DELIVERED = "delivered"  # channel
DELIVERY_FAILED = "delivery_failed"  # channel; detail: the error shown to the sender
OPENED = "opened"  # a recipient opened a code
DOWNLOADED = "downloaded"  # size: the file's bytes
CODE_FAILED = "code_failed"  # a wrong or expired code
PASSWORD_FAILED = "password_failed"


def visitor(ip: str, now: float) -> str:
    day = datetime.fromtimestamp(now, UTC).date().isoformat()
    return hashlib.sha256(f"{day}:{ip}".encode()).hexdigest()[:16]


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    ip: str | None = None,
    channel: str | None = None,
    size: int = 0,
    detail: str | None = None,
) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO events (ts, kind, channel, size, detail, visitor) VALUES (?, ?, ?, ?, ?, ?)",
        (now, kind, channel, size, detail, visitor(ip, now) if ip else None),
    )
