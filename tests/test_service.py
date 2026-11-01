import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.db import Database
from app.logic import OwnedGame
from app.service import NOTE_BASELINE, NOTE_PRIVATE, NOTE_REBASE, poll_once, simulate_purchase
from app.steam import PRIVATE_MESSAGE, AppDetails


class FakeNotifier:
    def __init__(self):
        self.batches = []

    async def deliver_pending(self):
        return 0

    async def deliver(self, events, digest=False):
        self.batches.append((digest, [event["appid"] for event in events]))
        return False


class FakeSteam:
    def __init__(self, owned):
        self.owned = owned
        self.details = {}

    async def get_owned_games(self, steam_id):
        return self.owned

    async def get_app_details(self, appid):
        return self.details.get(appid)


def _details(appid, kind="game", name="Игра"):
    return AppDetails(
        appid=appid,
        type=kind,
        name=name,
        short_description="Коротко.",
        header_image=f"https://example.com/{appid}.jpg",
        is_free=False,
        price_text="100 руб.",
    )


async def _ctx(db, steam):
    return SimpleNamespace(
        settings=SimpleNamespace(steam_enabled=True, demo_mode=True),
        db=db,
        http=None,
        notifier=FakeNotifier(),
        steam=steam,
        poll_lock=asyncio.Lock(),
        last_poll_at=None,
        last_poll_error=None,
    )


async def _friend(db, steam_id="76561198000000000"):
    await db.upsert_friend(
        steam_id=steam_id,
        persona_name="Катя",
        avatar_url="https://example.com/a.jpg",
        profile_url=f"https://steamcommunity.com/profiles/{steam_id}",
    )


def test_baseline_then_one_game_then_quiet(tmp_path):
    async def run():
        db = Database(tmp_path / "ping.db")
        await db.connect()
        try:
            await _friend(db)
            steam = FakeSteam([OwnedGame(10, "Старая"), OwnedGame(20, "Ещё")])
            steam.details[30] = _details(30, name="Новая")
            steam.details[40] = _details(40, "dlc", "Дополнение")
            ctx = await _ctx(db, steam)

            first = await poll_once(ctx)
            assert first.baselines == 1
            assert first.new_games == 0
            assert await db.count_notes_with_name("76561198000000000", NOTE_BASELINE) == 1

            steam.owned = [
                OwnedGame(10, "Старая"),
                OwnedGame(20, "Ещё"),
                OwnedGame(30, "Новая"),
                OwnedGame(40, "Дополнение"),
            ]
            second = await poll_once(ctx)
            assert second.new_games == 1
            games = [event for event in await db.list_events() if event["kind"] == "game"]
            assert [event["appid"] for event in games] == [30]
            assert ctx.notifier.batches == [(False, [30])]

            third = await poll_once(ctx)
            assert third.new_games == 0
            assert len(ctx.notifier.batches) == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_private_profile_notes_once(tmp_path):
    async def run():
        db = Database(tmp_path / "ping.db")
        await db.connect()
        try:
            await _friend(db)
            ctx = await _ctx(db, FakeSteam(None))
            await poll_once(ctx)
            await poll_once(ctx)
            friend = await db.get_friend("76561198000000000")
            assert friend["last_error"] == PRIVATE_MESSAGE
            assert friend["baseline_done"] == 0
            assert await db.count_notes_with_name("76561198000000000", NOTE_PRIVATE) == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_truncated_library_is_not_treated_as_purchases(tmp_path):
    async def run():
        db = Database(tmp_path / "ping.db")
        await db.connect()
        try:
            await _friend(db)
            full = [OwnedGame(i, f"Игра {i}") for i in range(1, 31)]
            steam = FakeSteam(full)
            ctx = await _ctx(db, steam)
            await poll_once(ctx)
            steam.owned = [OwnedGame(1, "Игра 1")]
            report = await poll_once(ctx)
            assert report.new_games == 0
            assert report.errors == 1
            friend = await db.get_friend("76561198000000000")
            assert "урезанный" in friend["last_error"]
            assert len(await db.known_appids("76561198000000000")) == 30
        finally:
            await db.close()

    asyncio.run(run())


def test_sudden_library_reveal_does_not_spam(tmp_path):
    async def run():
        db = Database(tmp_path / "ping.db")
        await db.connect()
        try:
            await _friend(db)
            steam = FakeSteam([])
            ctx = await _ctx(db, steam)
            await poll_once(ctx)
            steam.owned = [OwnedGame(i, f"Игра {i}") for i in range(1, 16)]
            for game in steam.owned:
                steam.details[game.appid] = _details(game.appid, name=game.name)
            report = await poll_once(ctx)
            assert report.new_games == 0
            assert report.rebaselines == 1
            assert await db.count_events_for("76561198000000000") == 0
            assert await db.count_notes_with_name("76561198000000000", NOTE_REBASE) == 1
            assert len(await db.known_appids("76561198000000000")) == 15
        finally:
            await db.close()

    asyncio.run(run())


def test_repeat_upsert_keeps_baseline(tmp_path):
    async def run():
        db = Database(tmp_path / "ping.db")
        await db.connect()
        try:
            await _friend(db)
            await db.set_check("76561198000000000", game_count=4, baseline=True, error=None)
            await db.upsert_friend(
                steam_id="76561198000000000",
                persona_name="Катя",
                avatar_url="",
                profile_url="",
            )
            friend = await db.get_friend("76561198000000000")
            assert friend["baseline_done"] == 1
            assert friend["game_count"] == 4
            assert await db.delete_friend("76561198000000000") is True
            assert await db.get_friend("76561198000000000") is None
        finally:
            await db.close()

    asyncio.run(run())


def test_simulate_purchase_uses_fallback_when_store_is_down(tmp_path):
    async def run():
        db = Database(tmp_path / "ping.db")
        await db.connect()

        class BoomClient:
            async def get(self, *args, **kwargs):
                raise httpx.ConnectError("нет сети")

        try:
            ctx = SimpleNamespace(
                db=db,
                http=BoomClient(),
                steam=None,
                settings=SimpleNamespace(demo_mode=True),
            )
            event = await simulate_purchase(ctx)
            assert event["is_demo"] == 1
            assert event["name"] == "Hades II"
            assert event["discord_sent"] == 2
            assert event["store_url"].startswith("https://store.steampowered.com/app/1145350")
        finally:
            await db.close()

    asyncio.run(run())
