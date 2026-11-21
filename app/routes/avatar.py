import asyncio
import time

from fastapi import APIRouter, HTTPException, Request, Response

from app.config import Settings
from app.db import connect
from app.routes.common import OwnerToken, get_owned_upload
from app.services.deliveries import DeliveryService

# Telegram limits username lookups, so photos (and "no photo") are kept for a day.
AVATAR_TTL = 24 * 3600

router = APIRouter()


@router.get("/uploads/{upload_id}/avatar")
async def recipient_avatar(upload_id: str, to: str, request: Request, owner_token: OwnerToken = "") -> Response:
    """Telegram profile photo of someone this upload was delivered to.

    Only the sender (owner token) of a successful delivery gets it, so the endpoint
    can't be used to look up arbitrary users.
    """
    settings: Settings = request.app.state.settings
    deliveries: DeliveryService = request.app.state.deliveries
    username = to.lower()

    def check() -> None:
        with connect(settings.db_path) as conn:
            get_owned_upload(conn, upload_id, owner_token)
            delivered = conn.execute(
                """
                SELECT 1 FROM deliveries
                WHERE upload_id = ? AND channel = 'telegram' AND status = 'sent' AND lower(recipient) = ?
                """,
                (upload_id, username),
            ).fetchone()
        if delivered is None:
            raise HTTPException(404, "Нет доставки этому получателю")

    await asyncio.to_thread(check)

    cache = settings.storage_dir / "avatars" / f"{username.lstrip('@')}.jpg"
    if cache.exists() and time.time() - cache.stat().st_mtime < AVATAR_TTL:
        photo = cache.read_bytes()
    else:
        sender = deliveries.senders.get("telegram")
        fetch = getattr(sender, "profile_photo", None)
        photo = (await fetch(to) if fetch and sender.available else None) or b""
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(photo)
    if not photo:
        raise HTTPException(404, "Фото нет")
    return Response(photo, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
