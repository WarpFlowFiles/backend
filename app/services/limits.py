import math
import sqlite3
import threading
from collections import defaultdict

from app.config import Settings


class UploadTracker:
    """Counts bytes of uploads still in progress, per IP.

    Kept in memory, so the server must run as a single process.
    """

    def __init__(self) -> None:
        self._bytes: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def add(self, ip: str, size: int) -> int:
        with self._lock:
            self._bytes[ip] += size
            return self._bytes[ip]

    def release(self, ip: str, size: int) -> None:
        with self._lock:
            self._bytes[ip] -= size
            if self._bytes[ip] <= 0:
                del self._bytes[ip]

    def get(self, ip: str) -> int:
        with self._lock:
            return self._bytes.get(ip, 0)

    def total(self) -> int:
        with self._lock:
            return sum(self._bytes.values())


def stored_bytes(conn: sqlite3.Connection, ip: str, now: float) -> int:
    """Total size of this IP's uploads that have not expired yet."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(f.size), 0)
        FROM files f JOIN uploads u ON u.id = f.upload_id
        WHERE u.ip = ? AND u.expires_at > ?
        """,
        (ip, now),
    ).fetchone()
    return row[0]


def retry_after(conn: sqlite3.Connection, ip: str, now: float, settings: Settings) -> int | None:
    """Seconds until this IP may try codes again, or None if it is not blocked."""
    count, oldest = conn.execute(
        "SELECT COUNT(*), MIN(timestamp) FROM failed_attempts WHERE ip = ? AND timestamp > ?",
        (ip, now - settings.failed_attempts_window),
    ).fetchone()
    if count < settings.max_failed_attempts:
        return None
    return max(1, math.ceil(oldest + settings.failed_attempts_window - now))


SEND_WINDOW = 60 * 60


def send_retry_after(
    conn: sqlite3.Connection, ip: str, channel: str, now: float, settings: Settings
) -> int | None:
    """Seconds until this IP may send via the channel again, or None if it is within the limit."""
    times = [
        row[0]
        for row in conn.execute(
            """
            SELECT created_at FROM deliveries
            WHERE ip = ? AND channel = ? AND created_at > ?
            ORDER BY created_at
            """,
            (ip, channel, now - SEND_WINDOW),
        )
    ]
    excess = len(times) - settings.max_sends_per_hour
    if excess < 0:
        return None
    # The window may hold more sends than the limit (e.g. after the limit was lowered):
    # a slot frees up only once all the extra ones have expired.
    return max(1, math.ceil(times[excess] + SEND_WINDOW - now))


def record_failure(conn: sqlite3.Connection, ip: str, now: float) -> None:
    conn.execute("INSERT INTO failed_attempts (ip, timestamp) VALUES (?, ?)", (ip, now))
