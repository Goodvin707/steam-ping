from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import httpx
import uvicorn

from app.config import load_settings
from app.context import Context
from app.discord_bot import build_bot, run_bot
from app.hub import GuildHub
from app.service import announce_if_needed, bootstrap, poll_all
from app.steam import SteamClient
from app.web import create_app

log = logging.getLogger("steam_ping")


async def poll_loop(ctx: Context) -> None:
    while True:
        try:
            report = await poll_all(ctx)
            if ctx.settings.steam_enabled:
                log.info(report.message(steam_enabled=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Цикл проверки продолжится после паузы")
        await asyncio.sleep(ctx.settings.poll_interval_seconds)


async def async_main() -> None:
    settings = load_settings()
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={
            "User-Agent": "SteamPing/1.0 (personal library watcher)",
            "Accept-Language": "ru",
        },
        follow_redirects=True,
    )
    hub = GuildHub(settings, http)
    local = await hub.start()
    ctx = Context.create(
        settings=settings,
        db=local.db,
        http=http,
        hub=hub,
        notifier=local.notifier,
        poll_lock=local.poll_lock,
    )
    if settings.steam_enabled:
        ctx.steam = SteamClient(settings.steam_api_key, http)
    await bootstrap(ctx)

    app = create_app(ctx)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
            access_log=False,
        )
    )
    poller = asyncio.create_task(poll_loop(ctx), name="poller")
    bot = None
    bot_task = None
    if settings.discord_enabled:
        bot = build_bot(ctx)
        bot_task = asyncio.create_task(run_bot(bot, settings.discord_token, ctx), name="discord")

    armed = await announce_if_needed(ctx)
    if armed:
        log.info(armed)
    elif await ctx.notifier.destination() == "panel":
        log.info("Автоотправка выключена: нет вебхука и нет канала.")

    shown_host = "127.0.0.1" if settings.web_host in {"0.0.0.0", "::"} else settings.web_host
    log.info("Панель: http://%s:%s", shown_host, settings.web_port)
    if settings.demo_mode:
        log.info("Демо включено: кнопка «Показать пример» собирает карточку без ключей.")
    if settings.invite_url:
        log.info("Приглашение бота: %s", settings.invite_url)

    try:
        await server.serve()
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.close()
        if bot_task is not None:
            bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bot_task
        await http.aclose()
        await hub.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
