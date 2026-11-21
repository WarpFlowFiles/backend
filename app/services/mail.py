import base64
import functools
import logging

import httpx

from app.config import Settings
from app.services.email_template import LOGO_CID, LOGO_PATH, render_email
from app.services.messages import (
    DeliveryError,
    OutgoingMessage,
    render_text,
    subject,
)

log = logging.getLogger(__name__)

UNAVAILABLE = "Сервис почты недоступен, попробуйте позже или отправьте через QR"
RECIPIENT_NOT_ALLOWED = "Отправка писем на этот адрес пока недоступна. Отправьте через QR или Telegram"
# Unisender Go: the free tier only delivers to addresses/domains marked as checked.
FREE_TIER_RECIPIENT = 903


class MailSender:
    """Sends email through the Unisender Go transactional API."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.available = bool(settings.unisender_api_key)

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=120, transport=self._transport)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def send(self, message: OutgoingMessage) -> bool:
        # Files are never attached: the letter has a download button for each of them.
        include_link = bool(message.files)
        payload = {
            "message": {
                "recipients": [{"email": message.recipient}],
                "subject": subject(message),
                "from_email": self._settings.mail_from,
                "from_name": "WarpFlow",
                "body": {
                    "plaintext": render_text(message, include_link),
                    "html": render_email(message),
                },
                "inline_attachments": [
                    {"type": "image/png", "name": LOGO_CID, "content": _logo_base64()},
                ],
                "track_links": 0,
                "track_read": 0,
            }
        }
        try:
            response = await self._client.post(
                self._settings.unisender_url,
                json=payload,
                headers={"X-API-KEY": self._settings.unisender_api_key},
            )
        except httpx.HTTPError:
            log.warning("Unisender Go request failed", exc_info=True)
            raise DeliveryError(UNAVAILABLE) from None

        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code != 200:
            log.warning("Unisender Go error %s: %s", response.status_code, response.text[:500])
            if data.get("code") == 204:  # every recipient was rejected
                raise DeliveryError("Адрес отклонён почтовым сервисом")
            if data.get("code") == FREE_TIER_RECIPIENT:
                log.warning(
                    "The Unisender Go free tier only sends to checked addresses: "
                    "switch to a paid tariff or mark %s as checked in the Unisender Go account",
                    message.recipient,
                )
                raise DeliveryError(RECIPIENT_NOT_ALLOWED)
            raise DeliveryError(UNAVAILABLE)
        if message.recipient in data.get("failed_emails", {}):
            raise DeliveryError("Адрес отклонён почтовым сервисом")
        return include_link


@functools.cache
def _logo_base64() -> str:
    return base64.b64encode(LOGO_PATH.read_bytes()).decode()
