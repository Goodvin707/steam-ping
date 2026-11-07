from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.db import Database
from app.notify import Notifier

log = logging.getLogger(__name__)


def _positive_id(value: int) -> int:
    guild_id = int(value)
    if guild_id <= 0:
        raise ValueError("Некорректный id сервера Discord")
    return guild_id


@dataclass
class Store:
    guild_id: int | None
    db: Database
    notifier: Notifier
    poll_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_poll_at: float | None = None
    last_poll_error: str | None = None


class Scope:
    """Срез Context с базой и каналом одного Discord-сервера."""

    def __init__(self, ctx, store: Store):
        self.settings = ctx.settings
        self.db = store.db
        self.http = ctx.http
        self.notifier = store.notifier
        self.steam = ctx.steam
        self.poll_lock = store.poll_lock
        self._ctx = ctx
        self._store = store
        self.guild_id = store.guild_id
        self.hub = None

    @property
    def last_poll_at(self):
        return self._store.last_poll_at

    @last_poll_at.setter
    def last_poll_at(self, value):
        self._store.last_poll_at = value

    @property
    def last_poll_error(self):
        return self._store.last_poll_error

    @last_poll_error.setter
    def last_poll_error(self, value):
        self._store.last_poll_error = value

    @property
    def discord_error(self):
        return self._ctx.discord_error


class GuildHub:
    """Отдельный файл SQLite на каждый Discord-сервер.

    Основная база остаётся у панели и у вебхука. Если сервер уже был единственным
    (или указан в DISCORD_GUILD_ID, или ему принадлежит сохранённый канал),
    он продолжает читать этот файл. Остальные серверы получают data/guilds/<id>.db.
    """

    def __init__(self, settings, http):
        self.settings = settings
        self.http = http
        self.bot = None
        self.local: Store | None = None
        self._dedicated: dict[int, Store] = {}
        self._lock = asyncio.Lock()
        self._split_logged = False

    async def start(self) -> Store:
        async with self._lock:
            if self.local is None:
                db = Database(self.settings.database_path)
                await db.connect()
                notifier = Notifier(db, self.http, self.settings, inherit_env=True)
                self.local = Store(guild_id=None, db=db, notifier=notifier)
            return self.local

    def attach_bot(self, bot) -> None:
        self.bot = bot
        if self.local is not None:
            self.local.notifier.bot = bot
        for store in self._dedicated.values():
            store.notifier.bot = bot

    def dedicated_path(self, guild_id: int) -> Path:
        guild_id = _positive_id(guild_id)
        return self.settings.database_path.parent / "guilds" / f"{guild_id}.db"

    def dedicated_ids(self) -> list[int]:
        directory = self.settings.database_path.parent / "guilds"
        if not directory.is_dir():
            return []
        ids = []
        for path in directory.glob("*.db"):
            if path.stem.isdigit() and int(path.stem) > 0:
                ids.append(int(path.stem))
        return sorted(ids)

    def _any_dedicated(self) -> bool:
        return bool(self.dedicated_ids())

    async def primary_id(self) -> int | None:
        if self.local is None:
            return None
        configured = getattr(self.settings, "discord_guild_id", None)
        if configured:
            return int(configured)
        raw = await self.local.db.get_setting("primary_guild_id")
        if raw and str(raw).isdigit() and int(raw) > 0:
            return int(raw)
        return None

    def resolved_path(self, guild_id: int, primary: int | None) -> Path:
        guild_id = _positive_id(guild_id)
        if primary is not None and guild_id == int(primary) and self.local is not None:
            return self.local.db.path
        return self.dedicated_path(guild_id)

    async def open_guild(self, guild_id: int) -> Store:
        await self.start()
        guild_id = _positive_id(guild_id)
        async with self._lock:
            await self._maybe_pin(guild_id)
            primary = await self.primary_id()
            path = self.resolved_path(guild_id, primary)
            assert self.local is not None
            if path.resolve() == self.local.db.path.resolve():
                return self.local
            existing = self._dedicated.get(guild_id)
            if existing is not None and existing.db.path.resolve() == path.resolve():
                return existing
            db = Database(path)
            await db.connect()
            notifier = Notifier(db, self.http, self.settings, inherit_env=False)
            notifier.bot = self.bot
            store = Store(guild_id=guild_id, db=db, notifier=notifier)
            self._dedicated[guild_id] = store
            log.info("Отдельная база сервера %s: %s", guild_id, path)
            return store

    async def scopes(self, ctx) -> list:
        scopes: list = [ctx]
        seen = {ctx.db.path.resolve()}
        for guild_id in self.dedicated_ids():
            store = await self.open_guild(guild_id)
            resolved = store.db.path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            scopes.append(Scope(ctx, store))
        return scopes

    async def summaries(self) -> list[dict]:
        await self.start()
        primary = await self.primary_id()
        names = self._joined_names()
        ids = set(self.dedicated_ids())
        ids.update(names)
        if primary is not None:
            ids.add(primary)
        assert self.local is not None
        local_path = self.local.db.path.resolve()
        rows = []
        for guild_id in sorted(ids):
            path = self.resolved_path(guild_id, primary)
            shared = path.resolve() == local_path
            if shared:
                friends = await self.local.db.list_friends()
                channel = await self._channel_for(self.local)
            elif path.exists():
                store = await self.open_guild(guild_id)
                friends = await store.db.list_friends()
                channel = await self._channel_for(store)
            else:
                friends = []
                channel = None
            live = sum(1 for friend in friends if not friend["is_demo"])
            rows.append(
                {
                    "guild_id": str(guild_id),
                    "name": names.get(guild_id) or f"Сервер {guild_id}",
                    "friends": len(friends),
                    "live_friends": live,
                    "notify_channel_id": channel,
                    "shared_with_panel": shared,
                }
            )
        return rows

    async def adopt_primary(self, bot) -> None:
        """Привязывает уже накопленные снимки к серверу, которому они принадлежали."""
        await self.start()
        async with self._lock:
            if await self.primary_id() is not None:
                return
            chosen = await self._match_existing_install(bot)
            if chosen is None:
                joined = list(getattr(bot, "guilds", []) or [])
                if len(joined) > 1 and not self._split_logged:
                    self._split_logged = True
                    log.info(
                        "Бот на нескольких серверах, канал уведомлений не выбран. "
                        "У каждого сервера своя база. Прежний список остаётся на панели."
                    )
                return
            assert self.local is not None
            await self.local.db.set_setting("primary_guild_id", str(chosen))
            log.info("Сервер %s использует текущую базу снимков и профилей.", chosen)

    async def close(self) -> None:
        stores = []
        if self.local is not None:
            stores.append(self.local)
        stores.extend(self._dedicated.values())
        seen: set[int] = set()
        for store in stores:
            marker = id(store.db)
            if marker in seen:
                continue
            seen.add(marker)
            await store.db.close()
        self._dedicated.clear()

    async def _maybe_pin(self, guild_id: int) -> None:
        if await self.primary_id() is not None:
            return
        chosen = await self._match_existing_install(self.bot)
        if chosen != guild_id:
            return
        assert self.local is not None
        await self.local.db.set_setting("primary_guild_id", str(guild_id))
        log.info("Сервер %s использует текущую базу снимков и профилей.", guild_id)

    async def _match_existing_install(self, bot) -> int | None:
        if self._any_dedicated() or bot is None:
            return None
        ready = getattr(bot, "is_ready", None)
        if ready is not None and not ready():
            return None
        assert self.local is not None
        channel_raw = await self.local.db.get_setting("notify_channel_id")
        if not channel_raw and getattr(self.settings, "notify_channel_id", None):
            channel_raw = str(self.settings.notify_channel_id)
        if channel_raw and str(channel_raw).isdigit():
            channel = bot.get_channel(int(channel_raw))
            if channel is None and hasattr(bot, "fetch_channel"):
                try:
                    channel = await bot.fetch_channel(int(channel_raw))
                except Exception:
                    channel = None
            guild = getattr(channel, "guild", None)
            if guild is not None:
                return int(guild.id)
        guilds = list(getattr(bot, "guilds", []) or [])
        if len(guilds) == 1:
            return int(guilds[0].id)
        return None

    def _joined_names(self) -> dict[int, str]:
        bot = self.bot
        if bot is None:
            return {}
        ready = getattr(bot, "is_ready", None)
        if ready is not None and not ready():
            return {}
        names = {}
        for guild in getattr(bot, "guilds", []) or []:
            title = str(getattr(guild, "name", "") or "").strip()
            names[int(guild.id)] = title or f"Сервер {guild.id}"
        return names

    async def _channel_for(self, store: Store) -> str | None:
        stored = await store.db.get_setting("notify_channel_id")
        if stored:
            return stored
        if store is self.local and getattr(self.settings, "notify_channel_id", None):
            return str(self.settings.notify_channel_id)
        return None
