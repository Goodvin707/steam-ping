import asyncio
from types import SimpleNamespace

import httpx

from app.context import Context
from app.hub import GuildHub
from app.logic import OwnedGame
from app.service import add_friend, poll_all
from app.web import create_app


class Spy:
    def __init__(self):
        self.batches = []

    async def deliver_pending(self):
        return 0

    async def deliver(self, events, digest=False):
        self.batches.append([event["appid"] for event in events])
        return True


class Steam:
    def __init__(self):
        self.owned = {}

    async def resolve(self, query):
        return str(query)

    async def get_player(self, steam_id):
        return SimpleNamespace(
            steam_id=str(steam_id),
            persona_name=f"Игрок {steam_id[-2:]}",
            avatar_url="",
            profile_url=f"https://steamcommunity.com/profiles/{steam_id}",
        )

    async def get_owned_games(self, steam_id):
        return self.owned[str(steam_id)]

    async def get_app_details(self, appid):
        return None


def _settings(path, **overrides):
    data = {
        "database_path": path,
        "discord_guild_id": None,
        "notify_channel_id": None,
        "discord_webhook_url": "",
        "discord_enabled": False,
        "steam_enabled": True,
        "demo_mode": False,
        "poll_interval_seconds": 600,
        "invite_url": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


async def _open(tmp_path, **overrides):
    settings = _settings(tmp_path / "steam_ping.db", **overrides)
    hub = GuildHub(settings, http=None)
    local = await hub.start()
    ctx = Context.create(
        settings=settings,
        db=local.db,
        http=None,
        hub=hub,
        notifier=local.notifier,
        poll_lock=local.poll_lock,
    )
    ctx.steam = Steam()
    return ctx


async def _friend(db, steam_id, name):
    await db.upsert_friend(
        steam_id=steam_id,
        persona_name=name,
        avatar_url="",
        profile_url=f"https://steamcommunity.com/profiles/{steam_id}",
    )


def test_each_guild_keeps_its_own_snapshots(tmp_path):
    async def run():
        ctx = await _open(tmp_path)
        try:
            left = await ctx.scope_for(101)
            right = await ctx.scope_for(202)
            assert left.db is not right.db
            assert left.db.path != right.db.path
            await _friend(left.db, "76561198000000001", "Аня")
            await _friend(right.db, "76561198000000002", "Боря")
            await left.db.add_owned("76561198000000001", [OwnedGame(10, "Старая")])
            await right.db.add_owned("76561198000000002", [OwnedGame(20, "Другая")])

            assert await left.db.known_appids("76561198000000001") == {10}
            assert await left.db.known_appids("76561198000000002") == set()
            assert await right.db.known_appids("76561198000000002") == {20}
            assert await right.db.known_appids("76561198000000001") == set()
            assert [friend["persona_name"] for friend in await left.db.list_friends()] == ["Аня"]
            assert [friend["persona_name"] for friend in await right.db.list_friends()] == ["Боря"]
            assert await ctx.db.list_friends() == []
            assert (tmp_path / "guilds" / "101.db").is_file()
            assert (tmp_path / "guilds" / "202.db").is_file()
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_configured_guild_keeps_the_main_database(tmp_path):
    async def run():
        ctx = await _open(tmp_path, discord_guild_id=101)
        try:
            await _friend(ctx.db, "76561198000000001", "Аня")
            home = await ctx.scope_for(101)
            other = await ctx.scope_for(202)
            assert home.db is ctx.db
            assert other.db is not ctx.db
            assert [friend["persona_name"] for friend in await home.db.list_friends()] == ["Аня"]
            assert await other.db.list_friends() == []
            assert not (tmp_path / "guilds" / "101.db").exists()
            assert (tmp_path / "guilds" / "202.db").is_file()
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_guild_notifier_does_not_inherit_webhook_or_channel(tmp_path):
    async def run():
        ctx = await _open(
            tmp_path,
            discord_webhook_url="https://discord.com/api/webhooks/7/env",
            notify_channel_id=42,
        )
        try:
            other = await ctx.scope_for(202)
            assert (await ctx.notifier.webhook_url()).endswith("/env")
            assert await ctx.notifier._channel_id() == "42"
            assert await other.notifier.webhook_url() == ""
            assert await other.notifier._channel_id() is None
            assert await other.notifier.destination() == "panel"
            assert "этого сервера" in await other.notifier.send_test()
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_existing_channel_pins_that_guild_to_the_current_database(tmp_path):
    async def run():
        ctx = await _open(tmp_path)
        try:
            await ctx.db.set_setting("notify_channel_id", "555")
            await _friend(ctx.db, "76561198000000001", "Аня")

            class Guild:
                def __init__(self, guild_id, name):
                    self.id = guild_id
                    self.name = name

            class Channel:
                def __init__(self, guild):
                    self.guild = guild

            home = Guild(101, "Домашний")
            other = Guild(202, "Чужой")

            class Bot:
                def __init__(self):
                    self.guilds = [home, other]

                def is_ready(self):
                    return True

                def get_channel(self, channel_id):
                    if channel_id == 555:
                        return Channel(home)
                    return None

            await ctx.hub.adopt_primary(Bot())
            assert await ctx.db.get_setting("primary_guild_id") == "101"
            assert (await ctx.scope_for(101)).db is ctx.db
            assert (await ctx.scope_for(202)).db is not ctx.db
            assert [friend["persona_name"] for friend in await (await ctx.scope_for(101)).db.list_friends()] == ["Аня"]
            assert await (await ctx.scope_for(202)).db.list_friends() == []
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_poll_checks_guilds_separately_and_delivers_only_there(tmp_path):
    async def run():
        ctx = await _open(tmp_path)
        try:
            left = await ctx.scope_for(101)
            right = await ctx.scope_for(202)
            await _friend(left.db, "76561198000000001", "Аня")
            await _friend(right.db, "76561198000000002", "Боря")
            spy_left = Spy()
            spy_right = Spy()
            ctx.hub._dedicated[101].notifier = spy_left
            ctx.hub._dedicated[202].notifier = spy_right
            ctx.steam.owned = {
                "76561198000000001": [OwnedGame(1, "Старая")],
                "76561198000000002": [OwnedGame(2, "Старая")],
            }

            first = await poll_all(ctx)
            assert first.baselines == 2
            assert first.friends_checked == 2
            assert spy_left.batches == []
            assert spy_right.batches == []

            ctx.steam.owned["76561198000000001"] = [
                OwnedGame(1, "Старая"),
                OwnedGame(30, "Новая"),
            ]
            second = await poll_all(ctx)
            assert second.new_games == 1
            assert second.friends_checked == 2
            assert spy_left.batches == [[30]]
            assert spy_right.batches == []
            assert await left.db.known_appids("76561198000000001") == {1, 30}
            assert await right.db.known_appids("76561198000000001") == set()
            assert await right.db.known_appids("76561198000000002") == {2}
            assert await ctx.db.known_appids("76561198000000001") == set()
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_home_guild_is_not_polled_twice(tmp_path):
    async def run():
        ctx = await _open(tmp_path, discord_guild_id=101)
        try:
            await _friend(ctx.db, "76561198000000001", "Аня")
            other = await ctx.scope_for(202)
            await _friend(other.db, "76561198000000002", "Боря")
            ctx.steam.owned = {
                "76561198000000001": [OwnedGame(1, "Старая")],
                "76561198000000002": [OwnedGame(2, "Старая")],
            }
            report = await poll_all(ctx)
            assert report.friends_checked == 2
            assert report.baselines == 2
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_commands_write_only_the_invoking_guild(tmp_path):
    async def run():
        ctx = await _open(tmp_path)
        try:
            left = await ctx.scope_for(101)
            right = await ctx.scope_for(202)
            await add_friend(left, "76561198000000001")
            await add_friend(right, "76561198000000002")
            assert [friend["steam_id"] for friend in await left.db.list_friends()] == ["76561198000000001"]
            assert [friend["steam_id"] for friend in await right.db.list_friends()] == ["76561198000000002"]
        finally:
            await ctx.hub.close()

    asyncio.run(run())


def test_panel_lists_other_guilds_without_mixing_friends(tmp_path):
    async def run():
        ctx = await _open(tmp_path, demo_mode=True, steam_enabled=False)
        try:
            await _friend(ctx.db, "76561198000000009", "Панель")
            other = await ctx.scope_for(202)
            await _friend(other.db, "76561198000000002", "Боря")
            app = create_app(ctx)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                status = (await client.get("/api/status")).json()
                friends = (await client.get("/api/friends")).json()
            assert [friend["persona_name"] for friend in friends] == ["Панель"]
            assert status["guilds"] == [
                {
                    "guild_id": "202",
                    "name": "Сервер 202",
                    "friends": 1,
                    "live_friends": 1,
                    "notify_channel_id": None,
                    "shared_with_panel": False,
                }
            ]
        finally:
            await ctx.hub.close()

    asyncio.run(run())
