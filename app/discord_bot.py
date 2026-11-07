from __future__ import annotations

import logging

import discord
from discord import app_commands

from app.logic import escape_md
from app.service import add_friend, announce_if_needed, poll_once, remove_friend

log = logging.getLogger(__name__)


def build_bot(ctx) -> discord.Client:
    bot = SteamPingBot(ctx)
    ctx.notifier.bot = bot
    if ctx.hub is not None:
        ctx.hub.attach_bot(bot)
    return bot


class SteamPingBot(discord.Client):
    def __init__(self, ctx):
        super().__init__(
            intents=discord.Intents.default(),
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Steam Libraries",
            ),
        )
        self.ctx = ctx
        self.tree = app_commands.CommandTree(self)
        self._synced_guilds: set[int] = set()
        _register(self)

    async def setup_hook(self) -> None:
        try:
            synced = await self.tree.sync()
            log.info(
                "Команды отправлены глобально: %s. На новом сервере появление может занять до часа.",
                len(synced),
            )
        except discord.HTTPException:
            log.exception("Не удалось зарегистрировать глобальные команды")
        guild_id = self.ctx.settings.discord_guild_id
        if guild_id:
            await self._sync_guild(int(guild_id))

    async def on_ready(self) -> None:
        log.info("Discord: вошёл как %s", self.user)
        self.ctx.discord_error = None
        if self.ctx.hub is not None:
            await self.ctx.hub.adopt_primary(self)
        for guild in self.guilds:
            await self._sync_guild(int(guild.id))
        try:
            await _deliver_ready(self.ctx)
        except Exception:
            log.exception("Не отправил очередь после подключения к Discord")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync_guild(int(guild.id))
        log.info("Бот добавлен на сервер %s. Список профилей и снимки у него свои.", guild.id)

    async def _sync_guild(self, guild_id: int) -> None:
        if guild_id in self._synced_guilds:
            return
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.exception("Не удалось зарегистрировать команды на сервере %s", guild_id)
            return
        self._synced_guilds.add(guild_id)
        log.info("Команды обновлены на сервере %s: %s", guild_id, len(synced))


def _register(bot: SteamPingBot) -> None:
    tree = bot.tree

    @tree.command(name="watch", description="Следить за библиотекой друга в Steam")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(steam="Ссылка на профиль, vanity или SteamID64")
    async def watch(interaction: discord.Interaction, steam: str) -> None:
        await interaction.response.defer(ephemeral=True)
        scope = await _scope(bot, interaction)
        try:
            friend = await add_friend(scope, steam)
        except Exception as exc:
            await interaction.followup.send(f"Не добавил: {exc}", ephemeral=True)
            return
        report = await _safe_poll(scope)
        await interaction.followup.send(
            (
                f"Слежу за **{friend['persona_name']}**. "
                "Дальше сверка идёт сама: сообщение уйдёт, когда появится новая игра.\n"
                f"{report}"
            ),
            ephemeral=True,
        )

    @tree.command(name="unwatch", description="Перестать следить за профилем")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(steam="Ник из списка, ссылка или SteamID64")
    async def unwatch(interaction: discord.Interaction, steam: str) -> None:
        await interaction.response.defer(ephemeral=True)
        scope = await _scope(bot, interaction)
        try:
            name = await remove_friend(scope, steam)
        except Exception as exc:
            await interaction.followup.send(f"Не убрал: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Больше не слежу за **{name}**.", ephemeral=True)

    @tree.command(name="list", description="Показать, за кем следит бот")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def list_friends(interaction: discord.Interaction) -> None:
        scope = await _scope(bot, interaction)
        friends = await scope.db.list_friends()
        if not friends:
            await interaction.response.send_message(
                "На этом сервере список пуст. Добавь друга командой /watch.",
                ephemeral=True,
            )
            return
        lines = []
        for friend in friends[:25]:
            mark = "пример" if friend["is_demo"] else ("слежу" if friend["baseline_done"] else "ещё не сверял")
            error = f" — {friend['last_error']}" if friend["last_error"] else ""
            lines.append(f"• **{friend['persona_name']}** ({mark}){error}")
        extra = len(friends) - len(lines)
        if extra > 0:
            lines.append(f"• и ещё {extra}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="channel", description="Писать уведомления в этот канал")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def channel(interaction: discord.Interaction) -> None:
        current = interaction.channel
        if current is None or interaction.guild is None or not hasattr(current, "send"):
            await interaction.response.send_message("Открой команду в текстовом канале сервера.", ephemeral=True)
            return
        me = interaction.guild.me
        if me is None:
            await interaction.response.send_message("Не вижу свою роль на сервере.", ephemeral=True)
            return
        perms = current.permissions_for(me)
        if not perms.send_messages or not perms.embed_links:
            await interaction.response.send_message(
                "В этом канале мне нужны права отправлять сообщения и вставлять ссылки.",
                ephemeral=True,
            )
            return
        scope = await _scope(bot, interaction)
        await scope.db.set_setting("notify_channel_id", str(current.id))
        note = await announce_if_needed(scope)
        follow = note or "Дальше новые игры этого сервера приходят сюда сами."
        await interaction.response.send_message(
            f"Сюда и буду писать сам: {current.mention}. {follow}",
            ephemeral=True,
        )
        await scope.notifier.deliver_pending()

    @tree.command(name="check", description="Проверить библиотеки прямо сейчас")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def check(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        scope = await _scope(bot, interaction)
        text = await _safe_poll(scope)
        await interaction.followup.send(text, ephemeral=True)

    @tree.command(name="status", description="Состояние бота и последней проверки")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def status(interaction: discord.Interaction) -> None:
        scope = await _scope(bot, interaction)
        friends = await scope.db.list_friends()
        live = sum(1 for friend in friends if not friend["is_demo"])
        channel_id = await scope.notifier._channel_id()
        where = await scope.notifier.destination()
        destination = {
            "discord": f"канал <#{channel_id}>",
            "webhook": "вебхук",
            "panel": "канал этого сервера не выбран",
        }[where]
        steam = "ключ на месте" if scope.settings.steam_enabled else "ключа нет"
        server = escape_md(interaction.guild.name) if interaction.guild is not None else "этот сервер"
        await interaction.response.send_message(
            (
                f"Сервер «{server}»: свой список и свои снимки.\n"
                f"Steam: {steam}. Живых профилей здесь: {live}.\n"
                f"Куда пишу: {destination}.\n"
                f"Интервал: {scope.settings.poll_interval_seconds} с."
            ),
            ephemeral=True,
        )

    @tree.command(name="test", description="Отправить в канал проверки тестовое сообщение")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def test(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        scope = await _scope(bot, interaction)
        text = await scope.notifier.send_test()
        await interaction.followup.send(text, ephemeral=True)


async def _scope(bot: SteamPingBot, interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return bot.ctx
    return await bot.ctx.scope_for(guild.id)


async def _safe_poll(scope) -> str:
    try:
        report = await poll_once(scope)
    except Exception:
        log.exception("Проверка из команды не удалась")
        return "Проверка сорвалась. Подробность в логе процесса."
    return report.message(steam_enabled=scope.settings.steam_enabled)


async def _deliver_ready(ctx) -> None:
    scopes = [ctx]
    if ctx.hub is not None:
        scopes = await ctx.hub.scopes(ctx)
    for scope in scopes:
        await announce_if_needed(scope)
        await scope.notifier.deliver_pending()


async def run_bot(bot: discord.Client, token: str, ctx) -> None:
    try:
        await bot.start(token)
    except discord.LoginFailure:
        ctx.discord_error = "Discord отклонил токен. Проверь DISCORD_TOKEN."
        log.error(ctx.discord_error)
    except Exception:
        ctx.discord_error = "Соединение с Discord оборвалось."
        log.exception(ctx.discord_error)
