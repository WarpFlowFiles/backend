"""Numbers for the stats site: the event log, what is stored right now, and the machine itself."""

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil

from app.config import Settings
from app.services import events as ev

CHANNELS = ("qr", "email", "telegram")
MAX_DAYS = 90


def collect(conn: sqlite3.Connection, settings: Settings, now: float, days: int) -> dict:
    tz = ZoneInfo(settings.timezone)
    local_now = datetime.fromtimestamp(now, tz)
    # SQLite has no time zones: shift timestamps by the zone's current offset.
    offset = int(local_now.utcoffset().total_seconds())
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = midnight - timedelta(days=days - 1)

    return {
        "generated_at": now,
        "timezone": settings.timezone,
        "today": _period(conn, midnight.timestamp()),
        "days": _days(conn, first_day, days, offset),
        "hours": _hours(conn, now, offset),
        "stored": _stored(conn, now),
        "telegram": _telegram(conn, midnight.timestamp()),
        "usage": _usage(conn, first_day.timestamp()),
        "failures": _failures(conn),
        "since": conn.execute("SELECT MIN(ts) FROM events").fetchone()[0],
    }


def _period(conn: sqlite3.Connection, start: float) -> dict:
    """Counts and bytes per kind (and channel for sends) since `start`."""
    out = {
        "visitors": 0,
        "senders": 0,
        "receivers": 0,
        "sent": {c: 0 for c in CHANNELS},
        "delivered": 0,
        "delivery_failed": 0,
        "files_uploaded": 0,
        "bytes_uploaded": 0,
        "opened": 0,
        "downloads": 0,
        "bytes_downloaded": 0,
        "code_failed": 0,
        "password_failed": 0,
    }
    rows = conn.execute(
        "SELECT kind, channel, COUNT(*), SUM(size) FROM events WHERE ts >= ? GROUP BY kind, channel", (start,)
    )
    for kind, channel, count, size in rows:
        if kind == ev.SENT and channel in out["sent"]:
            out["sent"][channel] = count
        elif kind == ev.FILE_UPLOADED:
            out["files_uploaded"], out["bytes_uploaded"] = count, size
        elif kind == ev.DOWNLOADED:
            out["downloads"], out["bytes_downloaded"] = count, size
        elif kind in (ev.DELIVERED, ev.DELIVERY_FAILED, ev.OPENED, ev.CODE_FAILED, ev.PASSWORD_FAILED):
            key = {ev.DELIVERED: "delivered", ev.DELIVERY_FAILED: "delivery_failed"}.get(kind, kind)
            out[key] += count
    out["visitors"], out["senders"], out["receivers"] = conn.execute(
        """
        SELECT COUNT(DISTINCT visitor),
               COUNT(DISTINCT CASE WHEN kind = ? THEN visitor END),
               COUNT(DISTINCT CASE WHEN kind = ? THEN visitor END)
        FROM events WHERE ts >= ?
        """,
        (ev.DRAFT, ev.OPENED, start),
    ).fetchone()
    return out


def _days(conn: sqlite3.Connection, first_day: datetime, days: int, offset: int) -> list[dict]:
    dates = [(first_day + timedelta(days=i)).date().isoformat() for i in range(days)]
    by_date = {
        d: {"date": d, "visitors": 0, "sent": {c: 0 for c in CHANNELS}, "files_uploaded": 0, "bytes_uploaded": 0,
            "downloads": 0, "bytes_downloaded": 0, "opened": 0}
        for d in dates
    }
    start = first_day.timestamp()
    rows = conn.execute(
        """
        SELECT date(ts + ?, 'unixepoch') AS day, kind, channel, COUNT(*), SUM(size)
        FROM events WHERE ts >= ? GROUP BY day, kind, channel
        """,
        (offset, start),
    )
    for day, kind, channel, count, size in rows:
        row = by_date.get(day)
        if row is None:
            continue
        if kind == ev.SENT and channel in row["sent"]:
            row["sent"][channel] = count
        elif kind == ev.FILE_UPLOADED:
            row["files_uploaded"], row["bytes_uploaded"] = count, size
        elif kind == ev.DOWNLOADED:
            row["downloads"], row["bytes_downloaded"] = count, size
        elif kind == ev.OPENED:
            row["opened"] = count
    for day, visitors in conn.execute(
        "SELECT date(ts + ?, 'unixepoch') AS day, COUNT(DISTINCT visitor) FROM events WHERE ts >= ? GROUP BY day",
        (offset, start),
    ):
        if day in by_date:
            by_date[day]["visitors"] = visitors
    return list(by_date.values())


def _hours(conn: sqlite3.Connection, now: float, offset: int) -> list[dict]:
    """The last 24 hours, the current one included."""
    hour = 3600
    current = (int(now) + offset) // hour * hour - offset
    starts = [current - hour * i for i in range(23, -1, -1)]
    by_start = {s: {"start": s, "sent": {c: 0 for c in CHANNELS}, "opened": 0, "downloads": 0} for s in starts}
    rows = conn.execute(
        """
        SELECT CAST((ts + ?) / 3600 AS INTEGER) * 3600 - ? AS hour, kind, channel, COUNT(*)
        FROM events WHERE ts >= ? AND kind IN (?, ?, ?) GROUP BY hour, kind, channel
        """,
        (offset, offset, starts[0], ev.SENT, ev.OPENED, ev.DOWNLOADED),
    )
    for start, kind, channel, count in rows:
        row = by_start.get(start)
        if row is None:
            continue
        if kind == ev.SENT and channel in row["sent"]:
            row["sent"][channel] = count
        elif kind == ev.OPENED:
            row["opened"] = count
        else:
            row["downloads"] = count
    return list(by_start.values())


def _stored(conn: sqlite3.Connection, now: float) -> dict:
    """What is on the server right now."""
    sent, drafts, qr, with_password, burn, expiring = conn.execute(
        """
        SELECT COUNT(CASE WHEN status = 'sent' THEN 1 END),
               COUNT(CASE WHEN status = 'draft' THEN 1 END),
               COUNT(CASE WHEN code_shared = 1 THEN 1 END),
               COUNT(CASE WHEN password_hash IS NOT NULL THEN 1 END),
               COUNT(CASE WHEN burn_after_download = 1 THEN 1 END),
               COUNT(CASE WHEN status = 'sent' AND expires_at <= ? THEN 1 END)
        FROM uploads WHERE expires_at > ?
        """,
        (now + 3600, now),
    ).fetchone()
    files, size, largest = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(f.size), 0), COALESCE(MAX(f.size), 0)
        FROM files f JOIN uploads u ON u.id = f.upload_id
        WHERE u.expires_at > ? AND f.downloaded_at IS NULL
        """,
        (now,),
    ).fetchone()
    pending = conn.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'pending'").fetchone()[0]
    return {
        "uploads": sent,
        "drafts": drafts,
        "qr_codes": qr,
        "with_password": with_password,
        "burn": burn,
        "expiring_hour": expiring,
        "files": files,
        "bytes": size,
        "largest_file": largest,
        "pending_deliveries": pending,
    }


def _telegram(conn: sqlite3.Connection, today: float) -> dict:
    recipients, stopped, greeted_today = conn.execute(
        """
        SELECT COUNT(*), COUNT(stopped_at), COUNT(CASE WHEN greeted_at >= ? THEN 1 END)
        FROM telegram_recipients
        """,
        (today,),
    ).fetchone()
    return {"recipients": recipients, "stopped": stopped, "new_today": greeted_today}


def _usage(conn: sqlite3.Connection, start: float) -> dict:
    """How QR codes are set up, over the chosen period."""
    total, with_password, burn = conn.execute(
        """
        SELECT COUNT(*),
               COUNT(CASE WHEN detail LIKE '%password%' THEN 1 END),
               COUNT(CASE WHEN detail LIKE '%burn%' THEN 1 END)
        FROM events WHERE kind = ? AND channel = 'qr' AND ts >= ?
        """,
        (ev.SENT, start),
    ).fetchone()
    return {"qr": total, "with_password": with_password, "burn": burn}


def _failures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, channel, detail FROM events WHERE kind = ? ORDER BY ts DESC LIMIT 10", (ev.DELIVERY_FAILED,)
    )
    return [{"ts": ts, "channel": channel, "error": detail} for ts, channel, detail in rows]


def server(settings: Settings) -> dict:
    """The machine: load, memory, disk under the storage folder, network since boot."""
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(settings.storage_dir.resolve()))
    net = psutil.net_io_counters()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpus": psutil.cpu_count(),
        "load": list(psutil.getloadavg()),
        "memory": {"used": memory.total - memory.available, "total": memory.total},
        "disk": {"used": disk.used, "total": disk.total, "free": disk.free},
        "database_bytes": _size(settings.db_path) + _size(Path(f"{settings.db_path}-wal")),
        "uptime": time.time() - psutil.boot_time(),
        "net": {"sent": net.bytes_sent, "received": net.bytes_recv},
    }


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
