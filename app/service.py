from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.demo import DEMO_CATALOG, DEMO_STEAM_ID, ensure_demo_friend
from app.logic import SKIP_APP_TYPES, Decision, decide
from app.steam import PRIVATE_MESSAGE, SteamError, header_fallback, store_url

log = logging.getLogger(__name__)

NOTE_BASELINE = "Список игр запомнен"
NOTE_REBASE = "Список обновлён без уведомлений"
NOTE_PRIVATE = "Библиотека скрыта"
TRUNCATED_MESSAGE = (
    "Steam вернул урезанный список игр. Снимок не менял, чтобы не принять сбой за покупки."
)


@dataclass
class PollReport:
    friends_checked: int = 0
    new_games: int = 0
    errors: int = 0
    rebaselines: int = 0
    baselines: int = 0

    def message(self, *, steam_enabled: bool) -> str:
        if not steam_enabled:
            return "Ключа Steam нет, живые профили не опрашиваются."
        if self.friends_checked == 0:
            return "В списке нет живых профилей. Демо-друг в Steam не ходит."
        parts = [f"Проверил профили: {self.friends_checked}."]
        if self.new_games:
            parts.append(f"Новых игр: {self.new_games}.")
        else:
            parts.append("Новых игр нет.")
        if self.baselines:
            parts.append(f"Впервые запомнил библиотеки: {self.baselines}.")
        if self.rebaselines:
            parts.append(f"Крупных обновлений без рассылки: {self.rebaselines}.")
        if self.errors:
            parts.append(f"Ошибок: {self.errors}.")
        return " ".join(parts)


def auto_label(
    *,
    delivery: str,
    steam_configured: bool,
    live_friends: int,
    poll_interval_seconds: int,
) -> str:
    if delivery == "panel":
        return (
            "Автоотправка выключена. Вставь вебхук канала ниже или выбери канал командой /channel. "
            "После этого новые игры уходят сами."
        )
    if not steam_configured:
        return "Канал подключён. Для автоотправки нужен ключ Steam и профиль друга."
    if live_friends <= 0:
        return "Канал подключён. Добавь профиль друга — дальше сообщения уходят сами."
    minutes = max(1, round(poll_interval_seconds / 60))
    return (
        f"Автоотправка включена. Библиотека сверяется сама, примерно раз в {minutes} мин. "
        "Новая игра уходит в Discord без кнопок и команд."
    )


def normalize_webhook(url: str) -> str:
    text = (url or "").strip()
    if not text.startswith(
        ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
    ):
        raise SteamError(
            "Нужен адрес вебхука Discord. Его копируют в канале: Интеграции → Вебхуки."
        )
    if len(text) > 300:
        raise SteamError("Слишком длинный адрес вебхука.")
    return text


async def announce_if_needed(ctx) -> str | None:
    """Один раз пишет в канал, что дальше сообщения пойдут сами."""
    async with ctx.notifier.send_lock:
        where = await ctx.notifier.destination()
        if where == "panel":
            return None
        channel_id = await ctx.notifier._channel_id()
        bot_pending = bool(
            where == "webhook"
            and ctx.settings.discord_enabled
            and ctx.notifier.bot is not None
            and not getattr(ctx, "discord_error", None)
            and channel_id
        )
        if bot_pending:
            return None
        marker = await ctx.notifier.delivery_marker()
        if await ctx.db.get_setting("auto_send_announced") == marker:
            return None
        from app.notify import armed_message

        if not await ctx.notifier._send_messages([armed_message()]):
            log.warning("Не удалось подтвердить автоотправку в Discord")
            return None
        await ctx.db.set_setting("auto_send_announced", marker)
        return (
            "Автоотправка включена. Проверочное сообщение уже в Discord, "
            "дальше новые игры уходят сами."
        )


async def set_webhook(ctx, url: str) -> str:
    clean = normalize_webhook(url)
    if not await ctx.notifier.probe_webhook(clean):
        raise SteamError("Discord не принял вебхук. Адрес не сохранён.")
    await ctx.db.set_setting("discord_webhook_url", clean)
    if await ctx.notifier.destination() == "webhook":
        await ctx.db.set_setting("auto_send_announced", await ctx.notifier.delivery_marker())
    return (
        "Автоотправка включена. Проверочное сообщение уже в канале. "
        "Дальше новая игра уходит сама, кнопку нажимать не нужно."
    )


async def clear_webhook(ctx) -> str:
    await ctx.db.set_setting("discord_webhook_url", "")
    if await ctx.notifier.destination() == "panel":
        await ctx.db.set_setting("auto_send_announced", "")
        return (
            "Автоотправка выключена. Новые игры остаются на панели, "
            "пока снова не подключишь канал."
        )
    return "Вебхук выключен. Автоотправка через бота в выбранный канал остаётся."


async def bootstrap(ctx) -> None:
    if ctx.settings.discord_webhook_url:
        await ctx.db.set_setting("discord_webhook_url", ctx.settings.discord_webhook_url)
    if ctx.settings.notify_channel_id:
        current = await ctx.db.get_setting("notify_channel_id")
        if not current:
            await ctx.db.set_setting(
                "notify_channel_id",
                str(ctx.settings.notify_channel_id),
            )
    if ctx.steam is not None:
        for query in ctx.settings.watch_steam_ids:
            try:
                await add_friend(ctx, query)
                log.info("В списке наблюдения: %s", query)
            except (SteamError, ValueError) as exc:
                log.warning("Не добавил %s: %s", query, exc)


async def add_friend(ctx, query: str) -> dict:
    if ctx.steam is None:
        raise SteamError("Нужен STEAM_API_KEY в файле .env.")
    steam_id = await ctx.steam.resolve(query)
    player = await ctx.steam.get_player(steam_id)
    return await ctx.db.upsert_friend(
        steam_id=player.steam_id,
        persona_name=player.persona_name,
        avatar_url=player.avatar_url,
        profile_url=player.profile_url,
        is_demo=False,
    )


async def remove_friend(ctx, query: str) -> str:
    friends = await ctx.db.list_friends()
    needle = query.strip().lower().rstrip("/")
    for friend in friends:
        if friend["steam_id"].lower() == needle or friend["persona_name"].lower() == needle:
            await ctx.db.delete_friend(friend["steam_id"])
            return friend["persona_name"]
        if friend["profile_url"].rstrip("/").lower() == needle:
            await ctx.db.delete_friend(friend["steam_id"])
            return friend["persona_name"]
    if ctx.steam is None:
        raise SteamError("Такого профиля в списке нет.")
    steam_id = await ctx.steam.resolve(query)
    friend = await ctx.db.get_friend(steam_id)
    if friend is None:
        raise SteamError("За этим профилем я не слежу.")
    await ctx.db.delete_friend(steam_id)
    return friend["persona_name"]


async def simulate_purchase(ctx) -> dict:
    friend = await ensure_demo_friend(ctx.db)
    raw_cursor = await ctx.db.get_setting("demo_cursor")
    cursor = int(raw_cursor) if raw_cursor and raw_cursor.isdigit() else 0
    appid, fallback_name, fallback_text = DEMO_CATALOG[cursor % len(DEMO_CATALOG)]
    await ctx.db.set_setting("demo_cursor", str(cursor + 1))

    details = await _details_for(ctx, appid)
    name = (details.name if details and details.name else fallback_name)
    description = (
        details.short_description
        if details and details.short_description
        else fallback_text
    )
    header = details.header_image if details and details.header_image else header_fallback(appid)
    price = details.price_text if details and details.price_text else "смотри в магазине"
    if friend["baseline_done"] == 0:
        await ctx.db.set_check(DEMO_STEAM_ID, game_count=0, baseline=True, error=None)
    return await ctx.db.add_event(
        steam_id=friend["steam_id"],
        persona_name=friend["persona_name"],
        avatar_url=friend["avatar_url"],
        appid=appid,
        name=name,
        header_image=header,
        short_description=description,
        price_text=price,
        store_url=store_url(appid),
        kind="game",
        is_demo=True,
        discord_sent=2,
    )


async def poll_once(ctx) -> PollReport:
    async with ctx.poll_lock:
        report = PollReport()
        try:
            await ctx.notifier.deliver_pending()
            if ctx.steam is None:
                return report
            ctx.last_poll_at = time.time()
            friends = await ctx.db.list_friends()
            for friend in friends:
                if friend["is_demo"]:
                    continue
                await _poll_friend(ctx, friend, report)
            ctx.last_poll_error = None
        except Exception:
            ctx.last_poll_error = "Проверка остановилась из-за внутренней ошибки."
            raise
        return report


async def _poll_friend(ctx, friend: dict, report: PollReport) -> None:
    report.friends_checked += 1
    steam_id = friend["steam_id"]
    try:
        owned = await ctx.steam.get_owned_games(steam_id)
    except SteamError as exc:
        await ctx.db.set_check(steam_id, error=str(exc))
        report.errors += 1
        log.info("%s: %s", friend["persona_name"], exc)
        return

    known = await ctx.db.known_appids(steam_id)
    decision: Decision = decide(
        baseline_done=bool(friend["baseline_done"]),
        previous_count=int(friend["game_count"]),
        known=known,
        owned=owned,
    )

    if decision.action == "private":
        report.errors += 1
        already = friend["last_error"] == PRIVATE_MESSAGE
        await ctx.db.set_check(steam_id, error=PRIVATE_MESSAGE)
        if not already:
            await _note(ctx, friend, NOTE_PRIVATE, PRIVATE_MESSAGE)
        return

    assert owned is not None
    if decision.action == "baseline":
        await ctx.db.add_owned(steam_id, owned)
        await ctx.db.set_check(steam_id, game_count=len(owned), baseline=True, error=None)
        await _note(
            ctx,
            friend,
            NOTE_BASELINE,
            f"В библиотеке сейчас {len(owned)} игр. Напишу, когда появится новая.",
        )
        report.baselines += 1
        log.info("%s: запомнил %s игр", friend["persona_name"], len(owned))
        return

    if decision.action == "truncated":
        await ctx.db.set_check(steam_id, error=TRUNCATED_MESSAGE)
        report.errors += 1
        log.warning("%s: %s", friend["persona_name"], TRUNCATED_MESSAGE)
        return

    if decision.action == "rebase":
        await ctx.db.add_owned(steam_id, owned)
        await ctx.db.set_check(steam_id, game_count=len(owned), error=None)
        await _note(
            ctx,
            friend,
            NOTE_REBASE,
            (
                f"За один заход появилось {len(decision.newcomers)} игр. "
                "Так бывает, когда библиотека только что открылась. "
                "Отдельные карточки не слал."
            ),
        )
        report.rebaselines += 1
        return

    if decision.action == "none":
        await ctx.db.set_check(steam_id, game_count=len(owned), error=None)
        return

    prepared = []
    for game in decision.newcomers:
        details = await _details_for(ctx, game.appid)
        if details is not None and details.type in SKIP_APP_TYPES:
            log.info("Пропускаю %s (%s), это %s", game.name, game.appid, details.type)
            continue
        prepared.append(
            await ctx.db.add_event(
                steam_id=steam_id,
                persona_name=friend["persona_name"],
                avatar_url=friend["avatar_url"] or "",
                appid=game.appid,
                name=(details.name if details and details.name else game.name),
                header_image=(
                    details.header_image if details and details.header_image else header_fallback(game.appid)
                ),
                short_description=details.short_description if details else "",
                price_text=details.price_text if details else "",
                store_url=store_url(game.appid),
                kind="game",
                is_demo=False,
                discord_sent=0,
            )
        )
    await ctx.db.add_owned(steam_id, owned)
    await ctx.db.set_check(steam_id, game_count=len(owned), error=None)
    if prepared:
        await ctx.notifier.deliver(prepared, digest=len(prepared) >= 6)
        report.new_games += len(prepared)
        log.info("%s: новых игр %s", friend["persona_name"], len(prepared))


async def _details_for(ctx, appid: int):
    if ctx.steam is not None:
        return await ctx.steam.get_app_details(appid)
    from app.steam import fetch_app_details

    return await fetch_app_details(ctx.http, appid)


async def _note(ctx, friend: dict, title: str, text: str) -> None:
    await ctx.db.add_event(
        steam_id=friend["steam_id"],
        persona_name=friend["persona_name"],
        avatar_url=friend["avatar_url"] or "",
        appid=0,
        name=title,
        short_description=text,
        kind="note",
        is_demo=bool(friend["is_demo"]),
        discord_sent=2,
    )
