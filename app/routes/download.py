import asyncio
import hmac
import mimetypes
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

from app.config import Settings
from app.db import connect
from app.routes.common import client_ip, resolve_upload
from app.schemas import FileInfo, UploadInfo
from app.services.events import DOWNLOADED, OPENED, PASSWORD_FAILED, record_event
from app.services.limits import record_failure
from app.services.passwords import check_password, file_key

PREVIEWABLE = {"image", "video", "audio"}

router = APIRouter()


# Percent-encoded: header values are Latin-1, and passwords may be in any script.
UploadPassword = Annotated[str, Header(alias="X-Upload-Password")]


@router.get("/uploads/{code}")
def get_upload(code: str, request: Request, password: UploadPassword = "") -> UploadInfo:
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    with connect(settings.db_path) as conn:
        upload = resolve_upload(conn, code, ip, settings)
        stored = upload["password_hash"]
        if stored is not None:
            if not password:
                raise HTTPException(401, "Введите пароль")
            if not check_password(unquote(password), stored):
                # Wrong passwords count towards the same attempt limit as wrong codes.
                record_failure(conn, ip, time.time())
                record_event(conn, PASSWORD_FAILED, ip=ip)
                conn.commit()
                raise HTTPException(401, "Неверный пароль")
        files = conn.execute(
            """
            SELECT id, original_name, size, downloaded_at FROM files
            WHERE upload_id = ? ORDER BY rowid
            """,
            (upload["id"],),
        ).fetchall()
        record_event(conn, OPENED, ip=ip)
        expires_at = upload["expires_at"]
        if upload["burn_after_download"] and not files:
            # Nothing to download: a text burns once it is read.
            expires_at = min(expires_at, time.time() + settings.burn_read_ttl)
            conn.execute("UPDATE uploads SET expires_at = ? WHERE id = ?", (expires_at, upload["id"]))
            record_taken(conn, upload["id"], time.time())
    return UploadInfo(
        code=upload["code"],
        from_name=upload["from_name"],
        text=upload["text"],
        expires_at=datetime.fromtimestamp(expires_at, UTC),
        burn_after_download=bool(upload["burn_after_download"]),
        files=[
            FileInfo(
                id=f["id"],
                name=f["original_name"],
                size=f["size"],
                downloaded=f["downloaded_at"] is not None,
            )
            for f in files
        ],
        key=file_key(stored, upload["id"]) if stored is not None else None,
    )


@router.get("/uploads/{code}/files/{file_id}")
def download_file(code: str, file_id: str, request: Request, preview: bool = False, key: str = "") -> FileResponse:
    """Serves a file; `preview` marks thumbnail and player requests, which never count as downloads.

    Files of a password-protected upload need the `key` that the upload info gave out.
    """
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    with connect(settings.db_path) as conn:
        upload = resolve_upload(conn, code, ip, settings)
        stored = upload["password_hash"]
        if stored is not None and not hmac.compare_digest(key.encode(), file_key(stored, upload["id"]).encode()):
            raise HTTPException(403, "Нужен пароль")
        file = conn.execute(
            "SELECT id, original_name, size, downloaded_at FROM files WHERE id = ? AND upload_id = ?",
            (file_id, upload["id"]),
        ).fetchone()
    if file is None:
        raise HTTPException(404, "Файл не найден")

    burn = bool(upload["burn_after_download"])
    if burn and file["downloaded_at"] is not None:
        raise HTTPException(410, "Файл уже скачан и удалён")

    path = settings.files_dir / upload["id"] / file["id"]
    kwargs = {
        "filename": file["original_name"],
        "media_type": media_type(file["original_name"]),
        "headers": {"X-Content-Type-Options": "nosniff"},
    }
    if preview:
        return FileResponse(path, **kwargs)

    def on_complete() -> None:
        if burn:
            mark_downloaded(settings.db_path, upload["id"], file["id"])
        with connect(settings.db_path) as conn:
            record_event(conn, DOWNLOADED, ip=ip, size=file["size"])

    return CompletionFileResponse(path, on_complete=on_complete, **kwargs)


class CompletionFileResponse(FileResponse):
    """Calls `on_complete` once the end of the file went out to the client.

    That is a whole 200 response, or a range response reaching the last byte (download
    managers on phones fetch files in ranges). Transfers cut short by a disconnect don't
    count: Starlette cancels the transfer then, so the final body chunk is never sent.
    """

    def __init__(self, path: Path, on_complete: Callable[[], None], **kwargs) -> None:
        super().__init__(path, **kwargs)
        self._on_complete = on_complete

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        reaches_end = False
        completed = False

        async def tracking_send(message: Message) -> None:
            nonlocal reaches_end, completed
            await send(message)
            if message["type"] == "http.response.start":
                reaches_end = _reaches_end(message)
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        await super().__call__(scope, receive, tracking_send)
        if completed and reaches_end and scope["method"] == "GET":
            await asyncio.to_thread(self._on_complete)


def _reaches_end(start: Message) -> bool:
    """Whether a response includes the file's last byte (a 200, or a 206 ending at it)."""
    if start["status"] == 200:
        return True
    if start["status"] != 206:
        return False
    headers = {k.lower(): v for k, v in start["headers"]}
    # "bytes 100-199/200"; multipart (several ranges) responses carry no such header.
    content_range = headers.get(b"content-range", b"").decode()
    span, _, size = content_range.removeprefix("bytes ").partition("/")
    _, _, end = span.partition("-")
    return end.isdigit() and size.isdigit() and int(end) == int(size) - 1


def mark_downloaded(db_path: Path, upload_id: str, file_id: str) -> None:
    """Marks the file as downloaded; once every file is, the upload expires immediately."""
    now = time.time()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE files SET downloaded_at = ? WHERE id = ? AND downloaded_at IS NULL", (now, file_id)
        )
        remaining = conn.execute(
            "SELECT 1 FROM files WHERE upload_id = ? AND downloaded_at IS NULL", (upload_id,)
        ).fetchone()
        if remaining is None:
            conn.execute("UPDATE uploads SET expires_at = MIN(expires_at, ?) WHERE id = ?", (now, upload_id))
            record_taken(conn, upload_id, now)


def record_taken(conn: sqlite3.Connection, upload_id: str, now: float) -> None:
    """Remembers that the recipient took the upload (the first time counts)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO taken_uploads (upload_id, owner_token_hash, taken_at)
        SELECT id, owner_token_hash, ? FROM uploads WHERE id = ?
        """,
        (now, upload_id),
    )


def media_type(filename: str) -> str:
    """Real type for media the page previews (Safari won't play octet-stream video).

    Everything else, including HTML and SVG, stays octet-stream so an uploaded
    file can never be rendered as a page of this site.
    """
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.split("/")[0] in PREVIEWABLE and guessed != "image/svg+xml":
        return guessed
    return "application/octet-stream"
