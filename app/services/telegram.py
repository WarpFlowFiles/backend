import logging
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from telethon import TelegramClient, errors, events
from telethon.tl.types import MessageEntityBold, User

from app.config import Settings
from app.db import connect
from app.services.messages import DeliveryError, OutgoingFile, OutgoingMessage, render_text

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_ALBUM = 10
SENDER_PREFIX = "От: "
# What Telegram shows in the chat as a photo or a playable video (bigger photos go as files).
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v"}
MAX_PHOTO_SIZE = 10 * 1024 * 1024
MEDIA_REJECTED = {
    "PHOTO_INVALID_DIMENSIONS",
    "PHOTO_EXT_INVALID",
    "PHOTO_SAVE_FILE_INVALID",
    "IMAGE_PROCESS_FAILED",
    "MEDIA_INVALID",
}
FORBIDDEN = "Пользователь запретил сообщения от незнакомых. Отправьте через QR или почту"
UNAVAILABLE = "Telegram временно недоступен. Отправьте через QR или почту"
NOT_A_CONTACT = (
    "Telegram не даёт нам написать первыми. Попросите получателя написать {account} любое сообщение "
    "и повторите отправку, или отправьте через QR или почту"
)
FORBIDDEN_RPC_MESSAGES = {"PRIVACY_PREMIUM_REQUIRED"}
PAID_MESSAGES = (
    "{user} принимает сообщения от незнакомых только за Telegram Stars{price}, "
    "поэтому бесплатно отправить не получится. Отправьте через QR или почту"
)
# Telegram reports "ALLOW_PAYMENT_REQUIRED_<stars per message>"; Telethon does not know this error.
PAID_MESSAGES_PREFIX = "ALLOW_PAYMENT_REQUIRED"
# Sent once before the first message to a person, so nobody keeps getting messages they do not
# want (and does not report the account as spam). No links: they look like spam to Telegram.
GREETING = (
    "Здравствуйте! Это служебный аккаунт WarpFlow — через него люди пересылают файлы и сообщения. "
    "Следующее сообщение — для вас.\n\n"
    "Если не хотите получать такие сообщения, напишите в этот чат /stop"
)
STOP_COMMANDS = {"/stop", "stop", "стоп"}
START_COMMANDS = {"/start", "start", "старт"}
STOPPED_REPLY = "Готово, больше не будем вам писать. Если передумаете, напишите /start"
RESTARTED_REPLY = "Снова можно присылать вам сообщения через WarpFlow. Отключить — /stop"
OPTED_OUT = "{user} отключил(а) сообщения от WarpFlow. Отправьте через QR или почту"


class TelegramSender:
    """Sends messages from the site's Telegram account via Telethon.

    The session file is created once with `python -m app.tg_login`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: TelegramClient | None = None
        self._account = "нашему аккаунту"
        self.available = False

    async def start(self) -> None:
        s = self._settings
        if not (s.telegram_api_id and s.telegram_api_hash and s.telegram_session.exists()):
            log.info("Telegram is not configured, sending via Telegram is disabled")
            return
        # catch_up: a /stop sent while the server was down still counts.
        client = TelegramClient(
            str(s.telegram_session),
            s.telegram_api_id,
            s.telegram_api_hash,
            catch_up=True,
            proxy=s.telegram_proxy_settings,
        )
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
        except Exception:
            log.exception("Cannot connect to Telegram")
            await client.disconnect()
            return
        if not authorized:
            log.warning("Telegram session is not authorized; run python -m app.tg_login")
            await client.disconnect()
            return
        try:
            me = await client.get_me()
        except Exception:
            log.warning("Cannot read the Telegram account username", exc_info=True)
            me = None
        if me and me.username:
            self._account = f"@{me.username}"
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self._client = client
        self.available = True

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def send(self, message: OutgoingMessage) -> bool:
        attach = bool(message.files) and all(
            f.size <= self._settings.telegram_file_limit for f in message.files
        )
        include_link = bool(message.files) and not attach
        text = render_text(message, include_link)
        try:
            user = await self._find_user(message.recipient)
            greeted, stopped = self._recipient(user.id)
            if stopped:
                raise DeliveryError(OPTED_OUT.format(user=message.recipient))
            if not greeted:
                await self._send_text(user, GREETING)
                self._update_recipient(user.id, "greeted_at", time.time())
            if not attach:
                await self._send_text(user, text)
                return include_link
            # Files and text go out as one message: the text is the caption of the last file
            # (under an album), unless it is too long for a caption.
            caption = text if len(text) <= MAX_CAPTION_LENGTH else ""
            if not caption:
                await self._send_text(user, text)
            groups = file_groups(message.files, message.compress_media)
            with named_copies(message.files, self._settings.tmp_dir) as paths:
                for i, (as_media, files) in enumerate(groups):
                    last = i == len(groups) - 1
                    await self._send_group(user, [paths[f.id] for f in files], as_media, caption if last else "")
            return include_link
        except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError, errors.YouBlockedUserError):
            raise DeliveryError(FORBIDDEN) from None
        except errors.PeerFloodError:
            # The account is spam-limited: it may only message users who wrote to it first.
            log.warning("Telegram account is spam-limited (see @SpamBot), cannot message %s", message.recipient)
            raise DeliveryError(NOT_A_CONTACT.format(account=self._account)) from None
        except errors.FloodWaitError:
            log.warning("Telegram rate limit hit", exc_info=True)
            raise DeliveryError(UNAVAILABLE) from None
        except errors.RPCError as e:
            if e.message in FORBIDDEN_RPC_MESSAGES:
                raise DeliveryError(FORBIDDEN) from None
            if e.message.startswith(PAID_MESSAGES_PREFIX):
                raise DeliveryError(paid_messages_error(message.recipient, e.message)) from None
            log.warning("Telegram error", exc_info=True)
            raise DeliveryError(UNAVAILABLE) from None

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """/stop from a person: never write to them again; /start: they changed their mind."""
        command = (event.raw_text or "").strip().lower()
        if command in STOP_COMMANDS:
            self._update_recipient(event.sender_id, "stopped_at", time.time())
            await event.reply(STOPPED_REPLY)
        elif command in START_COMMANDS and self._recipient(event.sender_id)[1]:
            self._update_recipient(event.sender_id, "stopped_at", None)
            await event.reply(RESTARTED_REPLY)

    def _recipient(self, user_id: int) -> tuple[bool, bool]:
        """Whether the person was greeted and whether they sent /stop."""
        with connect(self._settings.db_path) as conn:
            row = conn.execute(
                "SELECT greeted_at, stopped_at FROM telegram_recipients WHERE user_id = ?", (user_id,)
            ).fetchone()
        return (bool(row and row["greeted_at"]), bool(row and row["stopped_at"]))

    def _update_recipient(self, user_id: int, column: str, value: float | None) -> None:
        with connect(self._settings.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO telegram_recipients (user_id) VALUES (?)", (user_id,))
            conn.execute(f"UPDATE telegram_recipients SET {column} = ? WHERE user_id = ?", (value, user_id))

    async def _send_text(self, user: User, text: str) -> None:
        for i, chunk in enumerate(split_text(text)):
            entities = sender_bold(chunk) if i == 0 else []
            await self._client.send_message(
                user, chunk, link_preview=False, parse_mode=None, formatting_entities=entities
            )

    async def _send_group(self, user: User, paths: list[str], as_media: bool, caption: str) -> None:
        """One message or album: photos and videos shown in the chat, or files as they are."""
        captions = [""] * (len(paths) - 1) + [caption]
        entities = [[] for _ in paths[1:]] + [sender_bold(caption)]
        try:
            await self._client.send_file(
                user,
                paths if len(paths) > 1 else paths[0],
                caption=captions if len(paths) > 1 else caption,
                parse_mode=None,
                formatting_entities=entities if len(paths) > 1 else entities[0],
                force_document=not as_media,
                supports_streaming=as_media,
            )
        except errors.RPCError as e:
            # Telegram can refuse a picture it cannot process (odd proportions, a broken file):
            # then it still arrives, as a file.
            if not as_media or e.message not in MEDIA_REJECTED:
                raise
            log.info("Telegram rejected media (%s), sending as files", e.message)
            await self._send_group(user, paths, False, caption)

    async def profile_photo(self, username: str) -> bytes | None:
        """Small profile photo (JPEG) of a user, or None if they have none or it is hidden."""
        if self._client is None:
            return None
        try:
            user = await self._find_user(username)
            photo = await self._client.download_profile_photo(user, file=bytes, download_big=False)
        except (DeliveryError, errors.RPCError):
            log.info("No profile photo for %s", username, exc_info=True)
            return None
        return photo or None

    async def _find_user(self, username: str) -> User:
        not_found = DeliveryError(f"Пользователь {username} не найден в Telegram")
        try:
            entity = await self._client.get_entity(username)
        except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            raise not_found from None
        if not isinstance(entity, User) or entity.deleted:
            raise not_found
        return entity


def paid_messages_error(recipient: str, rpc_message: str) -> str:
    stars = rpc_message.removeprefix(PAID_MESSAGES_PREFIX).lstrip("_")
    price = f" ({stars} ⭐ за сообщение)" if stars.isdigit() else ""
    return PAID_MESSAGES.format(user=recipient, price=price)


def is_media(file: OutgoingFile) -> bool:
    ext = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
    return (ext in PHOTO_EXTENSIONS and file.size <= MAX_PHOTO_SIZE) or ext in VIDEO_EXTENSIONS


def file_groups(files: list[OutgoingFile], compress_media: bool) -> list[tuple[bool, list[OutgoingFile]]]:
    """Albums of up to 10: photos and videos first (when shown in the chat), then the other files.

    Returns (as_media, files) pairs; Telegram cannot mix media and documents in one album.
    """
    media = [f for f in files if compress_media and is_media(f)]
    documents = [f for f in files if f not in media]
    groups = []
    for as_media, kind in ((True, media), (False, documents)):
        groups += [(as_media, kind[i : i + MAX_ALBUM]) for i in range(0, len(kind), MAX_ALBUM)]
    return groups


@contextmanager
def named_copies(files: list[OutgoingFile], tmp_dir: Path) -> Iterator[dict[str, str]]:
    """Stored files are named by id; Telegram takes the name, type and preview from the path.

    Hard links under the real names cost nothing (a copy only where links are unsupported).
    """
    root = tmp_dir / f"tg-{uuid.uuid4().hex}"
    paths = {}
    try:
        for i, f in enumerate(files):
            folder = root / str(i)
            folder.mkdir(parents=True)
            target = folder / f.name
            try:
                os.link(f.path, target)
            except OSError:
                shutil.copyfile(f.path, target)
            paths[f.id] = str(target)
        yield paths
    finally:
        shutil.rmtree(root, ignore_errors=True)


def sender_bold(text: str) -> list[MessageEntityBold]:
    """The "От: имя" line in bold; the rest is sent as written (no Markdown parsing)."""
    if not text.startswith(SENDER_PREFIX):
        return []
    line = text.split("\n", 1)[0]
    # Telegram counts offsets in UTF-16 code units.
    return [MessageEntityBold(offset=0, length=len(line.encode("utf-16-le")) // 2)]


def split_text(text: str) -> list[str]:
    """Splits text into Telegram-sized messages, preferring line breaks."""
    chunks = []
    while len(text) > MAX_MESSAGE_LENGTH:
        cut = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if cut <= 0:
            cut = MAX_MESSAGE_LENGTH
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks
