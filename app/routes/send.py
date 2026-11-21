import hmac
import math
import re
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from app.config import Settings
from app.db import connect
from app.routes.common import OwnerToken, client_ip, get_owned_upload, hash_token
from app.schemas import DeliveryStatus, Pickup, SendRequest, SendResult
from app.services.deliveries import DeliveryService
from app.services.events import SENT, record_event
from app.services.limits import send_retry_after
from app.services.messages import DEFAULT_FROM_NAME
from app.services.passwords import MAX_LENGTH, MIN_LENGTH, hash_password, normalize

CHANNELS = {"qr", "email", "telegram"}
CHANNEL_NAMES = {"email": "Отправка по почте", "telegram": "Отправка в Telegram"}
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
TELEGRAM_PATTERN = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{3,31})")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_FROM_NAME_LENGTH = 64

router = APIRouter()


@router.post("/uploads/{upload_id}/send")
async def send_upload(
    upload_id: str, body: SendRequest, request: Request, owner_token: OwnerToken = ""
) -> SendResult:
    """Sends the upload via a channel.

    The first send freezes the text and sender name. The storage period and burn-after-download
    flag are the sender's choice once the code is handed out (a QR send); email and Telegram
    keep the files for the default period, which shrinks after a delivery that carried them.
    """
    settings: Settings = request.app.state.settings
    deliveries: DeliveryService = request.app.state.deliveries
    ip = client_ip(request)

    if body.channel not in CHANNELS:
        raise HTTPException(400, "Неизвестный способ отправки")
    recipient = _normalize_recipient(body.channel, body.to)
    from_name = _normalize_from_name(body.from_name)
    text = body.text.strip()
    if len(text) > settings.max_text_length:
        raise HTTPException(400, f"Текст длиннее {settings.max_text_length} символов")
    ttl = settings.sent_ttl if body.ttl is None else body.ttl
    if ttl not in settings.sent_ttl_options:
        raise HTTPException(400, "Недопустимый срок хранения")
    password_hash = _hash_password(body.channel, body.password)
    if body.channel != "qr" and not deliveries.is_available(body.channel):
        raise HTTPException(503, f"{CHANNEL_NAMES[body.channel]} сейчас недоступна, отправьте через QR")

    now = time.time()
    delivery_id = None
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        upload = get_owned_upload(conn, upload_id, owner_token)
        expires_at = upload["expires_at"]
        is_qr = body.channel == "qr"
        if not is_qr:
            ttl = settings.sent_ttl
        has_files = conn.execute("SELECT 1 FROM files WHERE upload_id = ?", (upload_id,)).fetchone()
        burn = is_qr and body.burn_after_download
        if upload["status"] == "draft":
            if not has_files and not text:
                raise HTTPException(400, "Добавьте файлы или текст")
            expires_at = now + ttl
            conn.execute(
                """
                UPDATE uploads
                SET status = 'sent', text = ?, from_name = ?, burn_after_download = ?, expires_at = ?,
                    code_shared = ?, password_hash = ?
                WHERE id = ?
                """,
                (text, from_name, burn, expires_at, is_qr, password_hash, upload_id),
            )
        elif not upload["code_shared"]:
            # Only delivered messages so far, which may have cut the expiry short: this send
            # starts it over (and a QR send sets the sender's own period).
            expires_at = now + ttl
            conn.execute(
                """
                UPDATE uploads SET burn_after_download = ?, expires_at = ?, code_shared = ?, password_hash = ?
                WHERE id = ?
                """,
                (burn, expires_at, is_qr, password_hash, upload_id),
            )
        if body.channel != "qr":
            wait = send_retry_after(conn, ip, body.channel, now, settings)
            if wait is not None:
                raise HTTPException(
                    429,
                    f"Лимит отправок исчерпан, попробуйте через {math.ceil(wait / 60)} мин.",
                    headers={"Retry-After": str(wait)},
                )
            delivery_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO deliveries (id, upload_id, channel, recipient, ip, created_at, compress_media)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (delivery_id, upload_id, body.channel, recipient, ip, now, body.compress_media),
            )
        flags = [name for name, on in (("password", password_hash), ("burn", burn)) if on]
        record_event(conn, SENT, ip=ip, channel=body.channel, detail=",".join(flags) or None)

    if delivery_id is not None:
        deliveries.schedule(delivery_id)
    return SendResult(
        code=upload["code"],
        expires_at=datetime.fromtimestamp(expires_at, UTC),
        delivery_id=delivery_id,
    )


@router.get("/uploads/{upload_id}/deliveries/{delivery_id}")
def get_delivery(
    upload_id: str, delivery_id: str, request: Request, owner_token: OwnerToken = ""
) -> DeliveryStatus:
    settings: Settings = request.app.state.settings
    with connect(settings.db_path) as conn:
        upload = get_owned_upload(conn, upload_id, owner_token)
        row = conn.execute(
            "SELECT * FROM deliveries WHERE id = ? AND upload_id = ?", (delivery_id, upload_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Отправка не найдена")
    return DeliveryStatus(
        id=row["id"],
        channel=row["channel"],
        recipient=row["recipient"],
        status=row["status"],
        error=row["error"],
        expires_at=datetime.fromtimestamp(upload["expires_at"], UTC),
        code_shared=bool(upload["code_shared"]),
    )


@router.get("/uploads/{upload_id}/pickup")
def get_pickup(upload_id: str, request: Request, owner_token: OwnerToken = "") -> Pickup:
    """How much of a burn-after-download upload the recipient took, also after it is gone."""
    settings: Settings = request.app.state.settings
    with connect(settings.db_path) as conn:
        taken = conn.execute(
            "SELECT owner_token_hash, taken_at FROM taken_uploads WHERE upload_id = ?", (upload_id,)
        ).fetchone()
        if taken is not None:
            if not hmac.compare_digest(taken["owner_token_hash"], hash_token(owner_token)):
                raise HTTPException(403, "Нет доступа к этой отправке")
            downloaded = conn.execute(
                "SELECT COUNT(*) FROM files WHERE upload_id = ? AND downloaded_at IS NOT NULL", (upload_id,)
            ).fetchone()[0]
            return Pickup(downloaded=downloaded, taken_at=datetime.fromtimestamp(taken["taken_at"], UTC))
        get_owned_upload(conn, upload_id, owner_token)
        downloaded = conn.execute(
            "SELECT COUNT(*) FROM files WHERE upload_id = ? AND downloaded_at IS NOT NULL", (upload_id,)
        ).fetchone()[0]
    return Pickup(downloaded=downloaded, taken_at=None)


def _normalize_recipient(channel: str, to: str) -> str:
    to = to.strip()
    if channel == "email":
        if len(to) > 254 or not EMAIL_PATTERN.fullmatch(to):
            raise HTTPException(400, "Введите корректный email")
        return to
    if channel == "telegram":
        match = TELEGRAM_PATTERN.fullmatch(to)
        if not match:
            raise HTTPException(400, "Введите имя пользователя Telegram, например @username")
        return f"@{match.group(1)}"
    return ""


def _hash_password(channel: str, password: str) -> str | None:
    """The hash to store; like the storage period, it is the QR sender's choice."""
    if not password:
        return None
    if channel != "qr":
        raise HTTPException(400, "Пароль можно поставить только на QR-код")
    if not MIN_LENGTH <= len(normalize(password)) <= MAX_LENGTH:
        raise HTTPException(400, f"Пароль должен быть от {MIN_LENGTH} до {MAX_LENGTH} символов")
    return hash_password(password)


def _normalize_from_name(name: str) -> str:
    name = CONTROL_CHARS.sub(" ", name).strip()
    if len(name) > MAX_FROM_NAME_LENGTH:
        raise HTTPException(400, f"Имя отправителя длиннее {MAX_FROM_NAME_LENGTH} символов")
    return name or DEFAULT_FROM_NAME
