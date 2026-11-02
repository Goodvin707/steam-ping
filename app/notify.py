from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.logic import clean_text, escape_md

log = logging.getLogger(__name__)

COLOR = 0x66C0F4
FOOTER = "Steam Ping · игра появилась в библиотеке"


def game_message(event: dict) -> tuple[str, dict[str, Any]]:
    persona = event["persona_name"]
    name = event["name"]
    content = (
        f"**{escape_md(persona)}** — в библиотеке Steam новая игра **{escape_md(name)}**."
    )
    description = clean_text(event.get("short_description"), 280)
    embed: dict[str, Any] = {
        "title": clean_text(name, 250) or "Новая игра",
        "url": event.get("store_url") or None,
        "description": description or "Страница магазина открывается по заголовку.",
        "color": COLOR,
        "author": {
            "name": clean_text(f"{persona} · библиотека Steam", 200),
            "icon_url": event.get("avatar_url") or None,
        },
        "fields": [
            {
                "name": "Вау!",
                "value": "Новая видеоигра",
                "inline": True,
            },
            {
                "name": "Цена на витрине",
                "value": clean_text(event.get("price_text"), 40) or "нет на витрине",
                "inline": True,
            },
        ],
        "footer": {"text": FOOTER},
    }
    if event.get("header_image"):
        embed["image"] = {"url": event["header_image"]}
    embed["author"] = {key: value for key, value in embed["author"].items() if value}
    if not embed.get("url"):
        embed.pop("url", None)
    return content, embed


def digest_message(events: list[dict]) -> tuple[str, dict[str, Any]]:
    persona = escape_md(events[0]["persona_name"])
    lines = []
    for event in events[:20]:
        url = event.get("store_url")
        title = escape_md(event["name"])
        if url:
            lines.append(f"• [{title}]({url})")
        else:
            lines.append(f"• {title}")
    hidden = len(events) - len(lines)
    if hidden > 0:
        lines.append(f"• и ещё {hidden}")
    content = (
        f"**{persona}** — в библиотеке Steam сразу появилось {len(events)} игр."
    )
    embed: dict[str, Any] = {
        "title": f"Новые игры: {len(events)}",
        "description": "\n".join(lines),
        "color": COLOR,
        "footer": {"text": FOOTER},
    }
    image = next((event["header_image"] for event in events if event.get("header_image")), "")
    if image:
        embed["image"] = {"url": image}
    return content, embed


def armed_message() -> tuple[str, dict[str, Any]]:
    return (
        "Автоотправка включена. Дальше Steam Ping сам пришлёт карточку, "
        "когда в открытой библиотеке друга появится новая игра.",
        {
            "title": "Канал на автоматической отправке",
            "description": (
                "Кнопку нажимать не нужно. Процесс сверяет библиотеку сам "
                "и пишет сюда только о новой игре."
            ),
            "color": COLOR,
            "footer": {"text": "Steam Ping"},
        },
    )


def test_message() -> tuple[str, dict[str, Any]]:
    return (
        "Проверка Steam Ping. Это не покупка и не событие из Steam.",
        {
            "title": "Канал уведомлений на связи",
            "description": (
                "Когда в открытой библиотеке друга появится новая игра, "
                "карточка придёт в этот канал."
            ),
            "color": COLOR,
            "footer": {"text": "Steam Ping"},
        },
    )


class Notifier:
    def __init__(self, db, http: httpx.AsyncClient, settings):
        self.db = db
        self.http = http
        self.settings = settings
        self.bot = None
        self.send_lock = asyncio.Lock()

    async def webhook_url(self) -> str:
        stored = await self.db.get_setting("discord_webhook_url")
        if stored is not None:
            return stored.strip()
        return (self.settings.discord_webhook_url or "").strip()

    async def delivery_marker(self) -> str:
        where = await self.destination()
        if where == "discord":
            return f"discord:{await self._channel_id()}"
        if where == "webhook":
            import hashlib

            digest = hashlib.sha256((await self.webhook_url()).encode()).hexdigest()[:16]
            return f"webhook:{digest}"
        return ""

    async def destination(self) -> str:
        channel_id = await self._channel_id()
        if channel_id and self.bot is not None and self.bot.is_ready():
            return "discord"
        if await self.webhook_url():
            return "webhook"
        return "panel"

    async def can_deliver(self) -> bool:
        return await self.destination() != "panel"

    async def deliver(self, events: list[dict], *, digest: bool = False) -> bool:
        async with self.send_lock:
            return await self._deliver(events, digest=digest)

    async def deliver_pending(self) -> int:
        async with self.send_lock:
            if await self.destination() == "panel":
                return 0
            pending = await self.db.pending_game_events()
            if not pending:
                return 0
            by_friend: dict[str, list[dict]] = {}
            for event in pending:
                by_friend.setdefault(event["steam_id"], []).append(event)
            sent = 0
            for batch in by_friend.values():
                if await self._deliver(batch, digest=len(batch) >= 6):
                    sent += len(batch)
            return sent

    async def probe_webhook(self, url: str) -> bool:
        async with self.send_lock:
            try:
                await self._post_webhook(url, [armed_message()])
            except Exception:
                log.warning("Вебхук не принял проверочное сообщение")
                return False
            return True

    async def send_test(self) -> str:
        async with self.send_lock:
            where = await self.destination()
            if where == "panel":
                return (
                    "Канал ещё не выбран. На сервере напиши /channel "
                    "или укажи DISCORD_WEBHOOK_URL."
                )
            ok = await self._send_messages([test_message()])
            if not ok:
                return "Сообщение собрать получилось, но Discord его не принял. Смотри лог процесса."
            if where == "discord":
                return "Тестовое сообщение ушло в выбранный канал."
            return "Тестовое сообщение ушло через вебхук."

    async def _deliver(self, events: list[dict], *, digest: bool = False) -> bool:
        real = [
            event
            for event in events
            if event.get("kind", "game") == "game" and not event.get("is_demo")
        ]
        if not real:
            return False
        if digest or len(real) >= 6:
            messages = [digest_message(real)]
        else:
            messages = [game_message(event) for event in real]
        ok = await self._send_messages(messages)
        await self.db.mark_events(
            [int(event["id"]) for event in real],
            1 if ok else 0,
        )
        return ok

    async def _channel_id(self) -> str | None:
        stored = await self.db.get_setting("notify_channel_id")
        if stored:
            return stored
        if self.settings.notify_channel_id:
            return str(self.settings.notify_channel_id)
        return None

    async def _send_messages(self, messages: list[tuple[str, dict]]) -> bool:
        where = await self.destination()
        try:
            if where == "discord":
                await self._send_bot(messages)
                return True
            if where == "webhook":
                await self._send_webhook(messages)
                return True
        except Exception:
            log.exception("Не удалось доставить сообщение в Discord")
            return False
        return False

    async def _send_bot(self, messages: list[tuple[str, dict]]) -> None:
        import discord

        channel_id = await self._channel_id()
        if not channel_id or self.bot is None:
            raise RuntimeError("Канал Discord не задан")
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(channel_id))
        for content, embed in messages:
            await channel.send(
                content=content,
                embed=discord.Embed.from_dict(embed),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _send_webhook(self, messages: list[tuple[str, dict]]) -> None:
        await self._post_webhook(await self.webhook_url(), messages)

    async def _post_webhook(self, url: str, messages: list[tuple[str, dict]]) -> None:
        if not url:
            raise RuntimeError("Вебхук не задан")
        for content, embed in messages:
            response = await self.http.post(
                url,
                json={
                    "content": content,
                    "embeds": [embed],
                    "username": "Steam Ping",
                    "allowed_mentions": {"parse": []},
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(f"вебхук ответил {response.status_code}")
