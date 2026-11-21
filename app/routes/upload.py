import asyncio
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from app.config import Settings, format_size
from app.db import connect
from app.routes.common import OwnerToken, client_ip, get_owned_upload, hash_token, require_draft
from app.schemas import DraftCreated, FileInfo, FilesAdded
from app.services.codes import generate_unique_code
from app.services.events import DRAFT, FILE_UPLOADED, record_event
from app.services.limits import stored_bytes
from app.services.receiver import MultipartReceiver, ReceivedFile, UploadRejected

# Allowance for multipart boundaries and headers when comparing Content-Length to the quota.
MULTIPART_OVERHEAD = 1024 * 1024

router = APIRouter()


@router.post("/uploads", status_code=201)
async def create_upload(request: Request) -> DraftCreated:
    """Creates a draft, optionally with files (a text-only draft has none)."""
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    upload_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + settings.draft_ttl

    def commit(conn: sqlite3.Connection, tmp_dir: Path | None, files: list[ReceivedFile]) -> None:
        conn.execute(
            """
            INSERT INTO uploads (id, code, ip, owner_token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (upload_id, generate_unique_code(conn), ip, hash_token(token), time.time(), expires_at),
        )
        _insert_files(conn, upload_id, files)
        record_event(conn, DRAFT, ip=ip)
        _record_files(conn, files, ip)
        dest = settings.files_dir / upload_id
        if tmp_dir is None:
            dest.mkdir()
        else:
            os.replace(tmp_dir, dest)

    has_body = request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
    if has_body:
        files = await _receive_and_commit(request, commit, require_body=False)
    else:
        files = []
        with connect(settings.db_path) as conn:
            commit(conn, None, files)

    return DraftCreated(
        id=upload_id,
        owner_token=token,
        expires_at=datetime.fromtimestamp(expires_at, UTC),
        files=[FileInfo(id=f.id, name=f.name, size=f.size) for f in files],
    )


@router.post("/uploads/{upload_id}/files", status_code=201)
async def add_files(upload_id: str, request: Request, owner_token: OwnerToken = "") -> FilesAdded:
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    with connect(settings.db_path) as conn:
        require_draft(get_owned_upload(conn, upload_id, owner_token))

    def commit(conn: sqlite3.Connection, tmp_dir: Path, files: list[ReceivedFile]) -> None:
        # The draft may have been sent, cancelled or expired while the files were uploading.
        require_draft(get_owned_upload(conn, upload_id, owner_token))
        conn.execute(
            "UPDATE uploads SET expires_at = ? WHERE id = ?",
            (time.time() + settings.draft_ttl, upload_id),
        )
        _insert_files(conn, upload_id, files)
        _record_files(conn, files, ip)
        for f in files:
            os.replace(tmp_dir / f.id, settings.files_dir / upload_id / f.id)

    files = await _receive_and_commit(request, commit, require_body=True)
    return FilesAdded(files=[FileInfo(id=f.id, name=f.name, size=f.size) for f in files])


@router.delete("/uploads/{upload_id}/files/{file_id}", status_code=204)
def delete_file(upload_id: str, file_id: str, request: Request, owner_token: OwnerToken = "") -> Response:
    settings: Settings = request.app.state.settings
    with connect(settings.db_path) as conn:
        require_draft(get_owned_upload(conn, upload_id, owner_token))
        deleted = conn.execute(
            "DELETE FROM files WHERE id = ? AND upload_id = ?", (file_id, upload_id)
        ).rowcount
    if not deleted:
        raise HTTPException(404, "Файл не найден")
    (settings.files_dir / upload_id / file_id).unlink(missing_ok=True)
    return Response(status_code=204)


@router.delete("/uploads/{upload_id}", status_code=204)
def cancel_upload(upload_id: str, request: Request, owner_token: OwnerToken = "") -> Response:
    """Makes the upload unavailable at once; the cleanup task deletes its files."""
    settings: Settings = request.app.state.settings
    with connect(settings.db_path) as conn:
        get_owned_upload(conn, upload_id, owner_token)
        conn.execute("UPDATE uploads SET expires_at = 0 WHERE id = ?", (upload_id,))
    return Response(status_code=204)


def _insert_files(conn: sqlite3.Connection, upload_id: str, files: list[ReceivedFile]) -> None:
    conn.executemany(
        "INSERT INTO files (id, upload_id, original_name, size) VALUES (?, ?, ?, ?)",
        [(f.id, upload_id, f.name, f.size) for f in files],
    )


def _record_files(conn: sqlite3.Connection, files: list[ReceivedFile], ip: str) -> None:
    for f in files:
        record_event(conn, FILE_UPLOADED, ip=ip, size=f.size)


async def _receive_and_commit(
    request: Request,
    commit: Callable[[sqlite3.Connection, Path, list[ReceivedFile]], None],
    require_body: bool,
) -> list[ReceivedFile]:
    """Streams the multipart body to a temp dir, then runs `commit` in a locked transaction.

    `commit(conn, tmp_dir, files)` must move the files out of `tmp_dir`.
    """
    settings: Settings = request.app.state.settings
    tracker = request.app.state.tracker
    ip = client_ip(request)

    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise HTTPException(400, "Ожидается multipart/form-data")

    with connect(settings.db_path) as conn:
        stored = stored_bytes(conn, ip, time.time())
    quota_error = f"Превышен лимит {format_size(settings.ip_quota)}. Дождитесь истечения прошлых отправок"
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit():
        free = settings.ip_quota - stored - tracker.get(ip)
        if int(content_length) - MULTIPART_OVERHEAD > free:
            raise HTTPException(413, quota_error)

    received = 0

    def on_bytes(size: int) -> None:
        nonlocal received
        received += size
        if stored + tracker.add(ip, size) > settings.ip_quota:
            raise UploadRejected(quota_error)

    tmp_dir = settings.tmp_dir / uuid.uuid4().hex
    tmp_dir.mkdir(parents=True)
    receiver = MultipartReceiver(boundary, tmp_dir, settings.max_file_size, on_bytes)
    try:
        async for chunk in request.stream():
            await asyncio.to_thread(receiver.write, chunk)
        receiver.finalize()
        receiver.close()
        if require_body and not receiver.files:
            raise HTTPException(400, "Выберите хотя бы один файл")

        with connect(settings.db_path) as conn:
            # Serializes commits, so concurrent uploads from one IP cannot both pass the quota check.
            conn.execute("BEGIN IMMEDIATE")
            total = sum(f.size for f in receiver.files)
            if stored_bytes(conn, ip, time.time()) + total > settings.ip_quota:
                raise UploadRejected(quota_error)
            commit(conn, tmp_dir, receiver.files)
        return receiver.files
    except UploadRejected as e:
        raise HTTPException(413, str(e)) from None
    except MultipartParseError:
        raise HTTPException(400, "Некорректный multipart-запрос") from None
    finally:
        receiver.close()
        tracker.release(ip, received)
        shutil.rmtree(tmp_dir, ignore_errors=True)
