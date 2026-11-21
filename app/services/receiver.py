import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from python_multipart.multipart import MultipartParser, parse_options_header

from app.config import format_size

MAX_NAME_LENGTH = 255
UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"]')


class UploadRejected(Exception):
    """The upload breaks a limit; the message is shown to the user."""


@dataclass
class ReceivedFile:
    id: str
    name: str
    size: int = 0


def clean_filename(raw: bytes) -> str:
    name = raw.decode("utf-8", errors="replace")
    name = re.split(r"[/\\]", name)[-1]
    name = UNSAFE_NAME_CHARS.sub("_", name).strip()
    return name[:MAX_NAME_LENGTH] or "file"


class MultipartReceiver:
    """Streams the file parts of a multipart body straight to disk.

    Files are saved in `dest` under random ids; other form fields are ignored.
    `on_bytes` is called for every written chunk and may raise UploadRejected.
    """

    def __init__(
        self,
        boundary: bytes,
        dest: Path,
        max_file_size: int,
        on_bytes: Callable[[int], None],
    ) -> None:
        self.files: list[ReceivedFile] = []
        self._dest = dest
        self._max_file_size = max_file_size
        self._on_bytes = on_bytes
        self._headers: dict[bytes, bytes] = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._current: ReceivedFile | None = None
        self._handle: BinaryIO | None = None
        self._parser = MultipartParser(
            boundary,
            callbacks={
                "on_part_begin": self._on_part_begin,
                "on_header_field": self._on_header_field,
                "on_header_value": self._on_header_value,
                "on_header_end": self._on_header_end,
                "on_headers_finished": self._on_headers_finished,
                "on_part_data": self._on_part_data,
                "on_part_end": self._on_part_end,
            },
        )

    def write(self, chunk: bytes) -> None:
        self._parser.write(chunk)

    def finalize(self) -> None:
        self._parser.finalize()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _on_part_begin(self) -> None:
        self._headers = {}
        self._current = None

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[bytes(self._header_field).lower()] = bytes(self._header_value)
        self._header_field.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition"))
        filename = options.get(b"filename")
        if not filename:
            return
        self._current = ReceivedFile(id=uuid.uuid4().hex, name=clean_filename(filename))
        self._handle = open(self._dest / self._current.id, "wb")

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current is None:
            return
        size = end - start
        self._current.size += size
        if self._current.size > self._max_file_size:
            raise UploadRejected(f"Файл больше {format_size(self._max_file_size)}")
        self._on_bytes(size)
        self._handle.write(data[start:end])

    def _on_part_end(self) -> None:
        if self._current is None:
            return
        self.close()
        self.files.append(self._current)
        self._current = None
