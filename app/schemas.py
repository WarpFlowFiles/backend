from datetime import datetime

from pydantic import BaseModel


class FileInfo(BaseModel):
    id: str
    name: str
    size: int
    downloaded: bool = False


class DraftCreated(BaseModel):
    id: str
    owner_token: str
    expires_at: datetime
    files: list[FileInfo]


class FilesAdded(BaseModel):
    files: list[FileInfo]


class SendRequest(BaseModel):
    channel: str
    to: str = ""
    from_name: str = ""
    text: str = ""
    # Seconds; like the text and sender name, applied on the first send only.
    ttl: int | None = None
    burn_after_download: bool = False
    # Telegram: photos and videos shown in the chat (Telegram compresses them), or sent as files.
    compress_media: bool = True
    # QR only: the recipient needs it besides the code.
    password: str = ""


class SendResult(BaseModel):
    code: str
    expires_at: datetime
    delivery_id: str | None = None


class DeliveryStatus(BaseModel):
    id: str
    channel: str
    recipient: str
    status: str
    error: str | None
    # The upload's expiry, cut short once a delivery carried the files itself.
    expires_at: datetime
    # The code was handed out (QR, or a link in a message), so it is worth showing.
    code_shared: bool


class Pickup(BaseModel):
    """What the recipient took of a burn-after-download upload, for the sender."""

    downloaded: int
    # Every file downloaded (or the text read): the upload is gone from the server.
    taken_at: datetime | None


class UploadInfo(BaseModel):
    code: str
    from_name: str
    text: str
    expires_at: datetime
    burn_after_download: bool
    files: list[FileInfo]
    # Password-protected uploads: goes with file links as `key`.
    key: str | None = None
