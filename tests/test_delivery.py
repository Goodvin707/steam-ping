import asyncio
from types import SimpleNamespace

import pytest

from app.db import Database
from app.notify import Notifier
from app.service import announce_if_needed, auto_label, clear_webhook, normalize_webhook, set_webhook
from app.steam import SteamError


class FakeHttp:
    def __init__(self, status=204):
        self.status = status
        self.posts = []

    async def post(self, url, json):
        self.posts.append((url, json["content"]))
        return SimpleNamespace(status_code=self.status)


def _settings(**overrides):
    data = {
        "discord_webhook_url": "",
        "notify_channel_id": None,
        "discord_enabled": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


async def _ready(tmp_path, http, settings):
    db = Database(tmp_path / "ping.db")
    await db.connect()
    notifier = Notifier(db, http, settings)
    ctx = SimpleNamespace(db=db, notifier=notifier, settings=settings, discord_error=None)
    return ctx


def test_normalize_webhook_rejects_other_hosts():
    with pytest.raises(SteamError):
        normalize_webhook("https://example.com/api/webhooks/1/token")
    url = "https://discord.com/api/webhooks/123/abc"
    assert normalize_webhook(f"  {url}  ") == url


def test_auto_label_explains_the_background_loop():
    text = auto_label(
        delivery="webhook",
        steam_configured=True,
        live_friends=1,
        poll_interval_seconds=600,
    )
    assert "Автоотправка включена" in text
    assert "10 мин" in text
    off = auto_label(
        delivery="panel",
        steam_configured=False,
        live_friends=0,
        poll_interval_seconds=600,
    )
    assert "выключена" in off


def test_saved_webhook_sends_once_and_then_stays_quiet(tmp_path):
    async def run():
        http = FakeHttp()
        ctx = await _ready(tmp_path, http, _settings())
        try:
            message = await set_webhook(ctx, "https://discord.com/api/webhooks/42/secret")
            assert "сама" in message
            assert http.posts[0][0].endswith("/secret")
            assert "Автоотправка включена" in http.posts[0][1]
            stored = await ctx.db.get_setting("discord_webhook_url")
            assert stored.endswith("/secret")

            again = await announce_if_needed(ctx)
            assert again is None
            assert len(http.posts) == 1
            assert await ctx.notifier.destination() == "webhook"
        finally:
            await ctx.db.close()

    asyncio.run(run())


def test_rejected_webhook_is_not_stored(tmp_path):
    async def run():
        http = FakeHttp(status=401)
        ctx = await _ready(tmp_path, http, _settings())
        try:
            with pytest.raises(SteamError):
                await set_webhook(ctx, "https://discord.com/api/webhooks/42/nope")
            assert await ctx.db.get_setting("discord_webhook_url") is None
            assert await ctx.notifier.destination() == "panel"
        finally:
            await ctx.db.close()

    asyncio.run(run())


def test_empty_database_value_does_not_fall_back_to_env(tmp_path):
    async def run():
        http = FakeHttp()
        settings = _settings(discord_webhook_url="https://discord.com/api/webhooks/7/env")
        ctx = await _ready(tmp_path, http, settings)
        try:
            assert await ctx.notifier.webhook_url() == settings.discord_webhook_url
            await clear_webhook(ctx)
            assert await ctx.notifier.webhook_url() == ""
            assert await ctx.notifier.destination() == "panel"
        finally:
            await ctx.db.close()

    asyncio.run(run())
