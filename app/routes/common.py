import hashlib
import hmac
import math
import sqlite3
import time
from typing import Annotated

from fastapi import Header, HTTPException, Request

from app.config import Settings
from app.services.codes import is_valid_code
from app.services.events import CODE_FAILED, record_event
from app.services.limits import record_failure, retry_after

OwnerToken = Annotated[str, Header(alias="X-Owner-Token")]


def client_ip(request: Request) -> str:
    # Behind a reverse proxy, run uvicorn with --proxy-headers so this is the real client.
    return request.client.host if request.client else "unknown"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_owned_upload(conn: sqlite3.Connection, upload_id: str, token: str) -> sqlite3.Row:
    """Finds an active upload and checks that the caller holds its owner token."""
    row = conn.execute(
        "SELECT * FROM uploads WHERE id = ? AND expires_at > ?", (upload_id, time.time())
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Отправка не найдена или срок действия истёк")
    if not hmac.compare_digest(row["owner_token_hash"], hash_token(token)):
        raise HTTPException(403, "Нет доступа к этой отправке")
    return row


def require_draft(upload: sqlite3.Row) -> None:
    if upload["status"] != "draft":
        raise HTTPException(409, "Отправка уже выполнена, изменить нельзя")


def resolve_upload(conn: sqlite3.Connection, code: str, ip: str, settings: Settings) -> sqlite3.Row:
    """Finds a sent, active upload by code, counting misses towards the IP's attempt limit."""
    now = time.time()
    wait = retry_after(conn, ip, now, settings)
    if wait is not None:
        raise HTTPException(
            429,
            f"Слишком много попыток, попробуйте через {math.ceil(wait / 60)} мин.",
            headers={"Retry-After": str(wait)},
        )

    row = None
    if is_valid_code(code):
        row = conn.execute(
            """
            SELECT id, code, text, from_name, burn_after_download, password_hash, expires_at FROM uploads
            WHERE code = ? AND status = 'sent' AND expires_at > ?
            """,
            (code, now),
        ).fetchone()
    if row is None:
        record_failure(conn, ip, now)
        record_event(conn, CODE_FAILED, ip=ip)
        conn.commit()
        raise HTTPException(404, "Код неверный или срок действия истёк")
    return row
