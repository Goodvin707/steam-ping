from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.db import Database
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

    @classmethod
    def create(cls, *, settings: Settings, db: Database, http: httpx.AsyncClient) -> Context:
        notifier = Notifier(db, http, settings)
        return cls(settings=settings, db=db, http=http, notifier=notifier)
