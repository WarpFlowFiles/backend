import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from telethon import errors

from app.config import Settings
from app.db import init_db
from app.services.mail import MailSender
from app.services.email_template import LOGO_CID
from app.services.messages import DeliveryError, OutgoingFile, OutgoingMessage, render_text, subject
from app.services.telegram import GREETING, TelegramSender, split_text


@pytest.fixture
def make_message(tmp_path):
    def _make(*files: tuple[str, bytes], text: str = "Привет") -> OutgoingMessage:
        outgoing = []
        for i, (name, content) in enumerate(files):
            path = tmp_path / f"file{i}"
            path.write_bytes(content)
            outgoing.append(OutgoingFile(name, path, len(content)))
        return OutgoingMessage(
            recipient="user@example.com",
            from_name="Маша",
            text=text,
            code="123456",
            link="https://warpflow.ru/r/123456",
            expires_at=time.time() + 24 * 3600,
            files=outgoing,
        )

    return _make


def test_render_text_with_and_without_link(make_message):
    message = make_message(("a.txt", b"a"))

    with_link = render_text(message, include_link=True)
    assert "От: Маша" in with_link and "Привет" in with_link
    assert "https://warpflow.ru/r/123456" in with_link and "123456" in with_link
    assert "24 ч." in with_link
    assert "https://warpflow.ru" not in render_text(message, include_link=False)


# --- Mail ---


def run_mail(message: OutgoingMessage, handler) -> list[dict]:
    """Sends the message through MailSender with a mocked Unisender Go; returns request payloads."""
    requests = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append({"headers": request.headers, "json": json.loads(request.content)})
        return handler(request)

    settings = Settings(unisender_api_key="key")
    sender = MailSender(settings, transport=httpx.MockTransport(record))

    async def main():
        await sender.start()
        try:
            await sender.send(message)
        finally:
            await sender.stop()

    asyncio.run(main())
    return requests


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "job_id": "1", "emails": ["user@example.com"]})


def test_mail_has_a_download_button_per_file(make_message):
    message = make_message(("a.txt", b"hello"), ("b.txt", b"bye"))
    [request] = run_mail(message, ok)

    assert request["headers"]["x-api-key"] == "key"
    payload = request["json"]["message"]
    assert payload["recipients"] == [{"email": "user@example.com"}]
    assert payload["from_email"] == "no-reply@warpflow.ru"
    assert "attachments" not in payload
    html = payload["body"]["html"]
    for f in message.files:
        assert f'href="https://warpflow.ru/api/uploads/123456/files/{f.id}"' in html
    assert html.count(">Скачать</a>") == 2
    assert "https://warpflow.ru/r/123456" in payload["body"]["plaintext"]


def test_mail_html_has_logo_and_escapes_user_input(make_message):
    message = make_message(("a.txt", b"hello"), text='<script>alert(1)</script>\nвторая строка')
    message = OutgoingMessage(**{**message.__dict__, "from_name": '<b>"Маша"</b>'})
    [request] = run_mail(message, ok)

    payload = request["json"]["message"]
    html = payload["body"]["html"]
    assert [a["name"] for a in payload["inline_attachments"]] == [LOGO_CID]
    assert base64.b64decode(payload["inline_attachments"][0]["content"]).startswith(b"\x89PNG")
    assert f"cid:{LOGO_CID}" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>" not in html and "&lt;b&gt;" in html
    assert "вторая строка" in html and "<br>" in html
    assert "a.txt" in html


def test_mail_subject(make_message):
    message = make_message(("a.txt", b"a"))
    assert subject(message) == "Маша: файлы через WarpFlow"
    anonymous = OutgoingMessage(**{**message.__dict__, "from_name": "Аноним", "files": []})
    assert subject(anonymous) == "Вам отправили сообщение через WarpFlow"


def test_mail_text_only(make_message):
    [request] = run_mail(make_message(), ok)

    payload = request["json"]["message"]
    assert "attachments" not in payload
    assert "сообщение" in payload["subject"]
    assert "warpflow.ru/r/" not in payload["body"]["plaintext"]


@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (
            lambda r: httpx.Response(200, json={"status": "success", "emails": [], "failed_emails": {"user@example.com": "invalid"}}),
            "Адрес отклонён",
        ),
        (lambda r: httpx.Response(400, json={"status": "error", "code": 204, "message": "no valid"}), "Адрес отклонён"),
        (
            lambda r: httpx.Response(403, json={"status": "error", "code": 903, "message": "free_tier ..."}),
            "на этот адрес пока недоступна",
        ),
        (lambda r: httpx.Response(500, text="oops"), "Сервис почты недоступен"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), "Сервис почты недоступен"),
    ],
)
def test_mail_errors(make_message, handler, error):
    with pytest.raises(DeliveryError, match=error):
        run_mail(make_message(("a.txt", b"a")), handler)


# --- Telegram ---


class FakeTelegramClient:
    def __init__(self, error: Exception | None = None, reject_media: str | None = None) -> None:
        self.error = error
        self.reject_media = reject_media
        self.messages: list[str] = []
        # Formatting of every text and caption, as sent.
        self.entities: list = []
        # One entry per message or album: (file names, captions, as media).
        self.sent: list[tuple[list[str], list[str], bool]] = []

    async def get_entity(self, username):
        from telethon.tl.types import User

        if username == "@nobody":
            raise ValueError("No user has 'nobody' as username")
        return User(id=1, username=username[1:])

    async def send_message(self, user, text, link_preview, parse_mode, formatting_entities):
        if self.error:
            raise self.error
        assert parse_mode is None
        self.messages.append(text)
        self.entities.append(formatting_entities)

    async def send_file(
        self, user, file, caption, force_document, supports_streaming, parse_mode, formatting_entities
    ):
        assert parse_mode is None
        self.entities.append(formatting_entities)
        if self.error:
            raise self.error
        if self.reject_media and not force_document:
            raise errors.RPCError(request=None, message=self.reject_media, code=400)
        files = file if isinstance(file, list) else [file]
        captions = caption if isinstance(caption, list) else [caption]
        # The named copies exist while sending: the real names and contents.
        names = [Path(f).name for f in files]
        assert all(Path(f).read_bytes() for f in files)
        self.sent.append((names, captions, not force_document))


def telegram_sender(client: FakeTelegramClient, tmp_path: Path, file_limit=10_000, greeted=True) -> TelegramSender:
    settings = Settings(telegram_file_limit=file_limit, storage_dir=tmp_path)
    init_db(settings.db_path)
    sender = TelegramSender(settings)
    sender._client = client
    # Most tests are about a person who already got the greeting (FakeTelegramClient's users are id 1).
    if greeted:
        sender._update_recipient(1, "greeted_at", time.time())
    return sender


def run_telegram(message: OutgoingMessage, client: FakeTelegramClient, tmp_path: Path) -> None:
    asyncio.run(telegram_sender(client, tmp_path).send(message))


def with_compress(message: OutgoingMessage, compress: bool) -> OutgoingMessage:
    return OutgoingMessage(**{**message.__dict__, "compress_media": compress})


def test_telegram_sends_files_with_the_text_as_one_message(make_message, tmp_path):
    client = FakeTelegramClient()
    run_telegram(make_message(("a.txt", b"hello")), client, tmp_path)

    assert client.messages == []
    [(names, captions, as_media)] = client.sent
    assert names == ["a.txt"] and not as_media
    assert "Привет" in captions[0] and "warpflow.ru/r/" not in captions[0]
    # The named copies are gone afterwards.
    assert not any((tmp_path / "tmp").iterdir())


def test_telegram_sender_line_is_bold_and_the_text_is_plain(make_message, tmp_path):
    from telethon.tl.types import MessageEntityBold

    client = FakeTelegramClient()
    run_telegram(make_message(), client, tmp_path)

    [text] = client.messages
    [[bold]] = client.entities
    assert isinstance(bold, MessageEntityBold) and bold.offset == 0
    assert text[: bold.length] == "От: Маша" and text.startswith("От: ")


def test_telegram_shows_photos_and_videos_in_an_album(make_message, tmp_path):
    client = FakeTelegramClient()
    message = make_message(("a.jpg", b"jpg"), ("b.mp4", b"mp4"), ("c.pdf", b"pdf"), ("d.png", b"png"))
    run_telegram(message, client, tmp_path)

    media, documents = client.sent
    assert media == (["a.jpg", "b.mp4", "d.png"], ["", "", ""], True)
    # The text goes under the last album, like a caption under the files.
    assert documents[0] == ["c.pdf"] and documents[2] is False and "Привет" in documents[1][0]


def test_telegram_sends_media_as_files_without_compression(make_message, tmp_path):
    client = FakeTelegramClient()
    run_telegram(with_compress(make_message(("a.jpg", b"jpg"), ("b.mp4", b"mp4")), False), client, tmp_path)

    [(names, _, as_media)] = client.sent
    assert names == ["a.jpg", "b.mp4"] and not as_media


def test_telegram_falls_back_to_files_when_media_is_rejected(make_message, tmp_path):
    client = FakeTelegramClient(reject_media="PHOTO_INVALID_DIMENSIONS")
    run_telegram(make_message(("a.jpg", b"jpg")), client, tmp_path)

    [(names, captions, as_media)] = client.sent
    assert names == ["a.jpg"] and not as_media and "Привет" in captions[0]


def test_telegram_albums_hold_ten_files(make_message, tmp_path):
    client = FakeTelegramClient()
    run_telegram(make_message(*[(f"{i}.jpg", b"x") for i in range(12)]), client, tmp_path)

    assert [len(names) for names, _, _ in client.sent] == [10, 2]


def test_telegram_long_text_goes_before_the_files(make_message, tmp_path):
    client = FakeTelegramClient()
    run_telegram(make_message(("a.txt", b"hello"), text="x" * 2000), client, tmp_path)

    assert len(client.messages) == 1 and "x" * 2000 in client.messages[0]
    assert client.sent == [(["a.txt"], [""], False)]


def test_telegram_sends_link_for_large_files(make_message, tmp_path):
    client = FakeTelegramClient()
    message = make_message(("big.bin", b"x" * 11))
    asyncio.run(telegram_sender(client, tmp_path, file_limit=10).send(message))

    assert "https://warpflow.ru/r/123456" in client.messages[0]
    assert client.sent == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (errors.UserPrivacyRestrictedError(request=None), "запретил сообщения"),
        (errors.RPCError(request=None, message="PRIVACY_PREMIUM_REQUIRED", code=403), "запретил сообщения"),
        (
            errors.RPCError(request=None, message="ALLOW_PAYMENT_REQUIRED_250", code=403),
            r"только за Telegram Stars \(250 ⭐ за сообщение\)",
        ),
        (errors.RPCError(request=None, message="ALLOW_PAYMENT_REQUIRED", code=400), "только за Telegram Stars,"),
        (errors.FloodWaitError(request=None, capture=30), "временно недоступен"),
        (errors.PeerFloodError(request=None), "написать нашему аккаунту любое сообщение"),
    ],
)
def test_telegram_errors(make_message, error, expected, tmp_path):
    with pytest.raises(DeliveryError, match=expected):
        run_telegram(make_message(), FakeTelegramClient(error), tmp_path)


def test_telegram_unknown_user(make_message, tmp_path):
    message = make_message()
    message = OutgoingMessage(**{**message.__dict__, "recipient": "@nobody"})
    with pytest.raises(DeliveryError, match="не найден"):
        run_telegram(message, FakeTelegramClient(), tmp_path)


class FakeEvent:
    def __init__(self, text: str) -> None:
        self.raw_text = text
        self.sender_id = 1
        self.replies: list[str] = []

    async def reply(self, text):
        self.replies.append(text)


def test_telegram_greets_a_person_once_before_the_first_message(make_message, tmp_path):
    client = FakeTelegramClient()
    sender = telegram_sender(client, tmp_path, greeted=False)

    asyncio.run(sender.send(make_message()))
    asyncio.run(sender.send(make_message()))

    assert client.messages[0] == GREETING and "/stop" in GREETING
    assert len(client.messages) == 3 and all(m.startswith("От: ") for m in client.messages[1:])


def test_telegram_stop_ends_messages_to_that_person(make_message, tmp_path):
    client = FakeTelegramClient()
    sender = telegram_sender(client, tmp_path)
    stop = FakeEvent(" /STOP ")
    asyncio.run(sender._on_message(stop))

    with pytest.raises(DeliveryError, match="отключил"):
        asyncio.run(sender.send(make_message()))
    assert client.messages == [] and "/start" in stop.replies[0]

    start = FakeEvent("/start")
    asyncio.run(sender._on_message(start))
    asyncio.run(sender.send(make_message()))
    assert len(start.replies) == 1 and len(client.messages) == 1


def test_telegram_ignores_other_messages(tmp_path):
    sender = telegram_sender(FakeTelegramClient(), tmp_path)
    for text in ("привет", "/start"):
        event = FakeEvent(text)
        asyncio.run(sender._on_message(event))
        assert event.replies == []
    assert sender._recipient(1) == (True, False)


def test_split_text():
    assert split_text("short") == ["short"]
    long_text = ("a" * 3000 + "\n") * 3
    chunks = split_text(long_text)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks).replace("\n", "") == long_text.replace("\n", "")
    assert [len(c) for c in split_text("b" * 5000)] == [4096, 904]


def test_telegram_proxy_setting(tmp_path):
    assert Settings(storage_dir=tmp_path).telegram_proxy_settings is None
    proxy = Settings(storage_dir=tmp_path, telegram_proxy="socks5://127.0.0.1:10808").telegram_proxy_settings
    assert proxy == {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 10808, "rdns": True}
    with_login = Settings(storage_dir=tmp_path, telegram_proxy="socks5://u:p%40ss@h:1").telegram_proxy_settings
    assert (with_login["username"], with_login["password"]) == ("u", "p@ss")
    with pytest.raises(ValueError):
        _ = Settings(storage_dir=tmp_path, telegram_proxy="mtproto://h").telegram_proxy_settings
