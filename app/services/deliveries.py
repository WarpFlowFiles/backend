import asyncio
import logging
import time

from app.config import Settings
from app.db import connect
from app.services.events import DELIVERED, DELIVERY_FAILED, record_event
from app.services.messages import DeliveryError, OutgoingFile, OutgoingMessage, Sender

log = logging.getLogger(__name__)

INTERRUPTED = "Не удалось отправить, попробуйте ещё раз"


class DeliveryService:
    """Runs email/Telegram deliveries in the background and records their outcome."""

    def __init__(self, settings: Settings, senders: dict[str, Sender]) -> None:
        self.settings = settings
        self.senders = senders
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await asyncio.to_thread(self._fail_pending)
        for sender in self.senders.values():
            await sender.start()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for sender in self.senders.values():
            await sender.stop()

    def is_available(self, channel: str) -> bool:
        sender = self.senders.get(channel)
        return sender is not None and sender.available

    def schedule(self, delivery_id: str) -> None:
        task = asyncio.create_task(self._run(delivery_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, delivery_id: str) -> None:
        status, error, linked = "sent", None, False
        try:
            channel, message = await asyncio.to_thread(self._load, delivery_id)
            linked = bool(await self.senders[channel].send(message))
        except DeliveryError as e:
            status, error = "failed", str(e)
        except asyncio.CancelledError:
            status, error = "failed", INTERRUPTED
            raise
        except Exception:
            log.exception("Delivery %s failed", delivery_id)
            status, error = "failed", INTERRUPTED
        finally:
            await asyncio.to_thread(self._finish, delivery_id, status, error, linked)

    def _load(self, delivery_id: str) -> tuple[str, OutgoingMessage]:
        with connect(self.settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT d.channel, d.recipient, d.compress_media, u.id, u.code, u.text, u.from_name, u.burn_after_download, u.expires_at
                FROM deliveries d JOIN uploads u ON u.id = d.upload_id
                WHERE d.id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise DeliveryError("Отправка была отменена")
            files = conn.execute(
                "SELECT id, original_name, size FROM files WHERE upload_id = ? ORDER BY rowid",
                (row["id"],),
            ).fetchall()
        upload_dir = self.settings.files_dir / row["id"]
        message = OutgoingMessage(
            recipient=row["recipient"],
            from_name=row["from_name"],
            text=row["text"],
            code=row["code"],
            link=f"{self.settings.public_url.rstrip('/')}/r/{row['code']}",
            expires_at=row["expires_at"],
            files=[OutgoingFile(f["original_name"], upload_dir / f["id"], f["size"]) for f in files],
            burn_after_download=bool(row["burn_after_download"]),
            compress_media=bool(row["compress_media"]),
        )
        return row["channel"], message

    def _finish(self, delivery_id: str, status: str, error: str | None, linked: bool) -> None:
        with connect(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE deliveries SET status = ?, error = ? WHERE id = ?",
                (status, error, delivery_id),
            )
            upload_id, channel = conn.execute(
                "SELECT upload_id, channel FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            kind = DELIVERED if status == "sent" else DELIVERY_FAILED
            record_event(conn, kind, channel=channel, detail=error)
            if status != "sent":
                return
            if linked:
                conn.execute("UPDATE uploads SET code_shared = 1 WHERE id = ?", (upload_id,))
                return
            # The message carried the text and files itself, so nobody needs the server copy:
            # it goes soon, unless the code was handed out or another message is on its way.
            conn.execute(
                """
                UPDATE uploads SET expires_at = MIN(expires_at, ?)
                WHERE id = ? AND code_shared = 0
                  AND NOT EXISTS (SELECT 1 FROM deliveries WHERE upload_id = ? AND status = 'pending')
                """,
                (time.time() + self.settings.delivered_ttl, upload_id, upload_id),
            )

    def _fail_pending(self) -> None:
        """Deliveries left pending by a previous run will never finish."""
        with connect(self.settings.db_path) as conn:
            conn.execute(
                "UPDATE deliveries SET status = 'failed', error = ? WHERE status = 'pending'",
                (INTERRUPTED,),
            )
