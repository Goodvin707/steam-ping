from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.db import Database
from app.hub import GuildHub, Scope
from app.notify import Notifier
from app.steam import SteamClient


@dataclass
class Context:
    settings: Settings
    db: Database
    http: httpx.AsyncClient
    notifier: Notifier
    steam: SteamClient | None = None
    poll_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_poll_at: float | None = None
    last_poll_error: str | None = None
    discord_error: str | None = None
    hub: GuildHub | None = None

    @classmethod
    def create(
        cls,
        *,
        settings: Settings,
        db: Database,
        http: httpx.AsyncClient,
        hub: GuildHub | None = None,
        notifier: Notifier | None = None,
        poll_lock: asyncio.Lock | None = None,
    ) -> Context:
        return cls(
            settings=settings,
            db=db,
            http=http,
            notifier=notifier or Notifier(db, http, settings),
            poll_lock=poll_lock or asyncio.Lock(),
            hub=hub,
        )

    async def scope_for(self, guild_id: int) -> Context | Scope:
        if self.hub is None:
            return self
        store = await self.hub.open_guild(int(guild_id))
        if store.db is self.db:
            return self
        return Scope(self, store)
