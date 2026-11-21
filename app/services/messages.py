import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_FROM_NAME = "Аноним"


class DeliveryError(Exception):
    """Delivery failed; the message is shown to the sender."""


@dataclass(frozen=True)
class OutgoingFile:
    name: str
    path: Path
    size: int

    @property
    def id(self) -> str:
        """Stored files are named by their id."""
        return self.path.name


@dataclass(frozen=True)
class OutgoingMessage:
    recipient: str
    from_name: str
    text: str
    code: str
    link: str
    expires_at: float
    files: list[OutgoingFile]
    burn_after_download: bool = False
    # Telegram: photos and videos shown in the chat (compressed), or sent as files.
    compress_media: bool = True

    def file_url(self, file: OutgoingFile) -> str:
        """Direct download of one file (the API serves it as an attachment)."""
        site = self.link.rsplit("/r/", 1)[0]
        return f"{site}/api/uploads/{self.code}/files/{file.id}"

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


class Sender(Protocol):
    available: bool

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, message: OutgoingMessage) -> bool:
        """Delivers the message; True when it carries a link to the files kept on the server."""
        ...


def subject(message: OutgoingMessage) -> str:
    what = "файлы" if message.files else "сообщение"
    if message.from_name == DEFAULT_FROM_NAME:
        return f"Вам отправили {what} через WarpFlow"
    return f"{message.from_name}: {what} через WarpFlow"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def time_left(message: OutgoingMessage) -> str:
    """How long the link works: "5 ч" up to two days, then "3 дня"."""
    hours = max(1, math.ceil((message.expires_at - time.time()) / 3600))
    if hours <= 48:
        return f"{hours} ч"
    return plural(math.ceil(hours / 24), "день", "дня", "дней")


def link_note(message: OutgoingMessage) -> str:
    note = f"Ссылка действует {time_left(message)}."
    if message.burn_after_download:
        note += " Файлы удалятся после скачивания."
    return note


def _link_lines(message: OutgoingMessage) -> tuple[str, str]:
    return f"Скачать файлы: {message.link}", f"Код: {message.code}. {link_note(message)}"


def render_text(message: OutgoingMessage, include_link: bool) -> str:
    parts = [f"От: {message.from_name}"]
    if message.text:
        parts.append(message.text)
    if include_link:
        parts.append("\n".join(_link_lines(message)))
    return "\n\n".join(parts)
