import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv

MB = 1024**2
GB = 1024**3


def format_size(size: float) -> str:
    """Like the frontend: "800 Б", "2,4 МБ", "15 МБ" (one decimal below 10)."""
    units = ("Б", "КБ", "МБ", "ГБ")
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    if unit == 0 or size >= 10:
        number = str(round(size))
    else:
        number = f"{round(size, 1):g}".replace(".", ",")
    return f"{number} {units[unit]}"


@dataclass(frozen=True)
class Settings:
    storage_dir: Path = Path("storage")
    public_url: str = "http://localhost:5173"
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])

    max_file_size: int = 5 * GB
    ip_quota: int = 5 * GB
    max_text_length: int = 4000
    draft_ttl: int = 60 * 60
    sent_ttl: int = 24 * 60 * 60
    # Storage periods the sender can choose from.
    sent_ttl_options: tuple[int, ...] = (60 * 60, 24 * 60 * 60, 3 * 24 * 60 * 60, 7 * 24 * 60 * 60)
    # A message that carried everything itself leaves the server copy only this long, in case
    # the sender sends the same thing to someone else.
    delivered_ttl: int = 10 * 60
    # A burn-after-reading text lives this long after it is first opened (a reload, a second
    # request of the page).
    burn_read_ttl: int = 60
    # How long the sender can still learn that a burn-after-download upload was taken.
    taken_ttl: int = 30 * 24 * 60 * 60
    # How far back the stats site can look.
    events_ttl: int = 90 * 24 * 60 * 60
    max_failed_attempts: int = 10
    failed_attempts_window: int = 15 * 60
    max_sends_per_hour: int = 5
    cleanup_interval: int = 60

    unisender_api_key: str = ""
    unisender_url: str = "https://goapi.unisender.ru/ru/transactional/api/v1/email/send.json"
    mail_from: str = "no-reply@warpflow.ru"

    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: Path = Path("telegram.session")
    # Telegram Premium allows files up to 4 GB.
    telegram_file_limit: int = 4000 * MB
    # Where Telegram is blocked: "socks5://host:port" (e.g. a local Xray, see deploy/vpn).
    telegram_proxy: str = ""

    # The stats site (app.stats): its password (it stays closed without one), where it finds
    # the main app, and the time zone of its days.
    stats_password: str = ""
    app_url: str = "http://127.0.0.1:8000"
    timezone: str = "Europe/Moscow"

    @property
    def telegram_proxy_settings(self) -> dict | None:
        """The proxy in Telethon's form, or None to connect directly."""
        if not self.telegram_proxy:
            return None
        url = urlsplit(self.telegram_proxy)
        if url.scheme not in ("socks5", "socks4", "http") or not url.hostname or not url.port:
            raise ValueError("TELEGRAM_PROXY must look like socks5://host:port")
        proxy = {"proxy_type": url.scheme, "addr": url.hostname, "port": url.port, "rdns": True}
        if url.username:
            proxy |= {"username": unquote(url.username), "password": unquote(url.password or "")}
        return proxy

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "warpflow.db"

    @property
    def files_dir(self) -> Path:
        return self.storage_dir / "files"

    @property
    def tmp_dir(self) -> Path:
        return self.storage_dir / "tmp"

    @classmethod
    def from_env(cls) -> "Settings":
        # Variables already set in the environment win over the .env file.
        load_dotenv(Path(".env"))
        env = {
            "storage_dir": ("WARPFLOW_STORAGE_DIR", Path),
            "public_url": ("WARPFLOW_PUBLIC_URL", str),
            "cors_origins": (
                "WARPFLOW_CORS_ORIGINS",
                lambda v: [o.strip() for o in v.split(",") if o.strip()],
            ),
            "unisender_api_key": ("UNISENDER_API_KEY", str),
            "unisender_url": ("UNISENDER_URL", str),
            "mail_from": ("WARPFLOW_MAIL_FROM", str),
            "max_sends_per_hour": ("WARPFLOW_MAX_SENDS_PER_HOUR", int),
            "telegram_api_id": ("TELEGRAM_API_ID", int),
            "telegram_api_hash": ("TELEGRAM_API_HASH", str),
            "telegram_session": ("TELEGRAM_SESSION", Path),
            "telegram_proxy": ("TELEGRAM_PROXY", str),
            "stats_password": ("WARPFLOW_STATS_PASSWORD", str),
            "app_url": ("WARPFLOW_APP_URL", str),
            "timezone": ("WARPFLOW_TIMEZONE", str),
        }
        overrides = {
            name: convert(value)
            for name, (var, convert) in env.items()
            if (value := os.getenv(var))
        }
        return cls(**overrides)
