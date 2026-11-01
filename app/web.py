from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.service import (
    add_friend,
    auto_label,
    clear_webhook,
    poll_once,
    remove_friend,
    set_webhook,
    simulate_purchase,
)
from app.steam import SteamError

STATIC = Path(__file__).resolve().parent / "static"


class WatchIn(BaseModel):
    query: str = Field(min_length=2, max_length=200)


class RemoveIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class WebhookIn(BaseModel):
    url: str = Field(min_length=8, max_length=300)


def create_app(ctx) -> FastAPI:
    app = FastAPI(title="Steam Ping", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        current = request.app.state.ctx
        bot = current.notifier.bot
        ready = bool(bot is not None and bot.is_ready())
        channel_id = await current.db.get_setting("notify_channel_id")
        friends = await current.db.list_friends()
        live_friends = sum(1 for friend in friends if not friend["is_demo"])
        delivery = await current.notifier.destination()
        return {
            "demo_mode": current.settings.demo_mode,
            "steam_configured": current.settings.steam_enabled,
            "discord_configured": current.settings.discord_enabled,
            "webhook_configured": bool(await current.notifier.webhook_url()),
            "discord_connected": ready,
            "discord_error": current.discord_error,
            "bot_user": str(bot.user) if ready and bot.user else None,
            "poll_interval_seconds": current.settings.poll_interval_seconds,
            "last_poll_at": current.last_poll_at,
            "last_poll_error": current.last_poll_error,
            "notify_channel_id": channel_id,
            "invite_url": current.settings.invite_url,
            "delivery": delivery,
            "friends": len(friends),
            "live_friends": live_friends,
            "auto_label": auto_label(
                delivery=delivery,
                steam_configured=current.settings.steam_enabled,
                live_friends=live_friends,
                poll_interval_seconds=current.settings.poll_interval_seconds,
            ),
        }

    @app.get("/api/friends")
    async def friends(request: Request) -> list[dict]:
        return await request.app.state.ctx.db.list_friends()

    @app.get("/api/events")
    async def events(request: Request) -> list[dict]:
        return await request.app.state.ctx.db.list_events()

    @app.post("/api/watch")
    async def watch(body: WatchIn, request: Request) -> dict:
        current = request.app.state.ctx
        query = body.query.strip()
        if len(query) < 2:
            raise HTTPException(status_code=400, detail="Нужна ссылка на профиль Steam.")
        try:
            friend = await add_friend(current, query)
        except (SteamError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            report = await poll_once(current)
            friend["poll_message"] = report.message(steam_enabled=current.settings.steam_enabled)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Профиль {friend['persona_name']} добавлен, но первая сверка сорвалась.",
            ) from exc
        return friend

    @app.post("/api/friends/remove")
    async def remove(body: RemoveIn, request: Request) -> dict:
        current = request.app.state.ctx
        try:
            name = await remove_friend(current, body.query)
        except (SteamError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"removed": name}

    @app.post("/api/demo/purchase")
    async def purchase(request: Request) -> dict:
        current = request.app.state.ctx
        if not current.settings.demo_mode:
            raise HTTPException(
                status_code=400,
                detail="Пример выключен: заданы и Steam, и Discord. DEMO_MODE=true вернёт кнопку.",
            )
        return await simulate_purchase(current)

    @app.post("/api/check")
    async def check(request: Request) -> dict:
        current = request.app.state.ctx
        try:
            report = await poll_once(current)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Проверка сорвалась. Подробность в логе.") from exc
        return {
            "friends_checked": report.friends_checked,
            "new_games": report.new_games,
            "errors": report.errors,
            "rebaselines": report.rebaselines,
            "baselines": report.baselines,
            "message": report.message(steam_enabled=current.settings.steam_enabled),
        }

    @app.post("/api/webhook")
    async def save_webhook(body: WebhookIn, request: Request) -> dict:
        current = request.app.state.ctx
        try:
            message = await set_webhook(current, body.url)
        except (SteamError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"message": message}

    @app.post("/api/webhook/clear")
    async def forget_webhook(request: Request) -> dict:
        message = await clear_webhook(request.app.state.ctx)
        return {"message": message}

    @app.post("/api/test-delivery")
    async def test_delivery(request: Request) -> dict:
        text = await request.app.state.ctx.notifier.send_test()
        return {"message": text}

    return app
