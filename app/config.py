from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

log = logging.getLogger(__name__)


def client_id_from_token(token: str) -> str:
    part = token.split(".", 1)[0]
    if not part:
        return ""
    padded = part + "=" * (-len(part) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            text = decoder(padded).decode("ascii")
        except Exception:
            continue
        if text.isdigit():
            return text
    return ""


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        log.warning("%s должен быть числом, значение проигнорировано", name)
        return None
    return int(raw)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_guild_id: int | None
    discord_client_id: str
    notify_channel_id: int | None
    discord_webhook_url: str
    steam_api_key: str
    poll_interval_seconds: int
    database_path: Path
    web_host: str
    web_port: int
    demo_mode: bool
    watch_steam_ids: tuple[str, ...]

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_token)

    @property
    def steam_enabled(self) -> bool:
        return bool(self.steam_api_key)

    @property
    def invite_url(self) -> str | None:
        client_id = self.discord_client_id or client_id_from_token(self.discord_token)
        if not client_id:
            return None
        return (
            "https://discord.com/api/oauth2/authorize"
            f"?client_id={client_id}&permissions=19456"
            "&scope=bot%20applications.commands"
        )


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    steam_key = os.getenv("STEAM_API_KEY", "").strip()
    demo_raw = os.getenv("DEMO_MODE", "auto").strip().lower()
    if demo_raw in {"", "auto"}:
        demo_mode = not (token and steam_key)
    else:
        demo_mode = demo_raw in {"1", "true", "yes", "on"}

    try:
        interval = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))
    except ValueError:
        interval = 600
    interval = min(max(interval, 60), 24 * 60 * 60)

    try:
        port = int(os.getenv("WEB_PORT", "8741"))
    except ValueError:
        port = 8741

    database = os.getenv("DATABASE_PATH", "").strip()
    database_path = Path(database) if database else ROOT / "data" / "steam_ping.db"
    if not database_path.is_absolute():
        database_path = ROOT / database_path

    watches = tuple(
        part.strip()
        for part in os.getenv("WATCH_STEAM_IDS", "").split(",")
        if part.strip()
    )
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if webhook and not webhook.startswith(
        ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
    ):
        log.warning("DISCORD_WEBHOOK_URL не похож на вебхук Discord, значение проигнорировано")
        webhook = ""

    explicit_client = os.getenv("DISCORD_CLIENT_ID", "").strip()
    if explicit_client and not explicit_client.isdigit():
        log.warning("DISCORD_CLIENT_ID должен быть числом, значение проигнорировано")
        explicit_client = ""

    return Settings(
        discord_token=token,
        discord_guild_id=_optional_int("DISCORD_GUILD_ID"),
        discord_client_id=explicit_client,
        notify_channel_id=_optional_int("NOTIFY_CHANNEL_ID"),
        discord_webhook_url=webhook,
        steam_api_key=steam_key,
        poll_interval_seconds=interval,
        database_path=database_path,
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        web_port=port,
        demo_mode=demo_mode,
        watch_steam_ids=watches,
    )
