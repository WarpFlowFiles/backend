import asyncio
import logging
import shutil
import time

from app.config import Settings
from app.db import connect
from app.services.limits import SEND_WINDOW

log = logging.getLogger(__name__)


def cleanup_expired(settings: Settings, now: float) -> int:
    """Deletes expired uploads and old failed attempts; returns the number of uploads removed.

    An upload whose files cannot be deleted yet (e.g. still being downloaded)
    keeps its row, so the next pass retries it.
    """
    with connect(settings.db_path) as conn:
        expired = [row["id"] for row in conn.execute("SELECT id FROM uploads WHERE expires_at <= ?", (now,))]

    removed = 0
    for upload_id in expired:
        try:
            shutil.rmtree(settings.files_dir / upload_id)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Cannot delete files of upload %s yet", upload_id, exc_info=True)
            continue
        with connect(settings.db_path) as conn:
            conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        removed += 1

    with connect(settings.db_path) as conn:
        conn.execute(
            "DELETE FROM failed_attempts WHERE timestamp <= ?",
            (now - settings.failed_attempts_window,),
        )
        conn.execute("DELETE FROM taken_uploads WHERE taken_at <= ?", (now - settings.taken_ttl,))
        conn.execute("DELETE FROM events WHERE ts <= ?", (now - settings.events_ttl,))
        # Deliveries of deleted uploads are kept only while they count towards the send limit.
        conn.execute(
            """
            DELETE FROM deliveries
            WHERE created_at <= ? AND upload_id NOT IN (SELECT id FROM uploads)
            """,
            (now - SEND_WINDOW,),
        )
    return removed


def clear_tmp(settings: Settings) -> None:
    """Removes uploads left unfinished by a previous run."""
    shutil.rmtree(settings.tmp_dir, ignore_errors=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)


async def run_cleanup_loop(settings: Settings) -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_expired, settings, time.time())
        except Exception:
            log.exception("Cleanup failed")
        await asyncio.sleep(settings.cleanup_interval)
