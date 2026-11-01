from __future__ import annotations

import html
import re
from dataclasses import dataclass

STEAMID64_RE = re.compile(r"7656\d{13}")
VANITY_RE = re.compile(r"[A-Za-z0-9_-]{2,32}")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

SKIP_APP_TYPES = frozenset(
    {"dlc", "music", "video", "hardware", "advertising", "mod", "demo"}
)


class SteamQueryError(ValueError):
    """Ссылка или ник не разобрались как профиль Steam."""


def parse_steam_query(raw: str) -> tuple[str, str]:
    """Вернуть ('steamid' | 'vanity', значение)."""
    text = (raw or "").strip()
    if not text:
        raise SteamQueryError("Пустая ссылка. Нужен профиль Steam, vanity или SteamID64.")

    profile = re.search(
        r"steamcommunity\.com/profiles/(7656\d{13})",
        text,
        re.IGNORECASE,
    )
    if profile:
        return "steamid", profile.group(1)

    vanity = re.search(
        r"steamcommunity\.com/id/([A-Za-z0-9_-]{2,32})",
        text,
        re.IGNORECASE,
    )
    if vanity:
        return "vanity", vanity.group(1)

    if STEAMID64_RE.fullmatch(text):
        return "steamid", text
    if VANITY_RE.fullmatch(text):
        return "vanity", text
    raise SteamQueryError(
        "Не понял ссылку. Нужен профиль Steam, vanity или SteamID64."
    )


def clean_text(value: str | None, limit: int) -> str:
    text = html.unescape(TAG_RE.sub(" ", value or ""))
    text = SPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def looks_truncated(previous_count: int, new_count: int) -> bool:
    """Короткий ответ Steam иногда означает сбой, а не распродажу библиотеки."""
    if previous_count >= 10 and new_count == 0:
        return True
    if previous_count >= 20 and new_count < previous_count * 0.5:
        return True
    return False


def classify_newcomers(previous_count: int, newcomers: int) -> str:
    """none | each | digest | rebase."""
    if newcomers <= 0:
        return "none"
    if newcomers >= 21:
        return "rebase"
    if previous_count <= 2 and newcomers >= 12:
        return "rebase"
    if newcomers >= 6:
        return "digest"
    return "each"


@dataclass(frozen=True)
class OwnedGame:
    appid: int
    name: str
    playtime_forever: int = 0


@dataclass(frozen=True)
class Decision:
    action: str
    newcomers: tuple[OwnedGame, ...] = ()


def decide(
    *,
    baseline_done: bool,
    previous_count: int,
    known: set[int],
    owned: list[OwnedGame] | None,
) -> Decision:
    if owned is None:
        return Decision("private")
    if not baseline_done:
        return Decision("baseline")
    if looks_truncated(previous_count, len(owned)):
        return Decision("truncated")
    newcomers = tuple(game for game in owned if game.appid not in known)
    action = classify_newcomers(previous_count, len(newcomers))
    return Decision(action, newcomers)


def parse_owned_payload(payload: dict) -> list[OwnedGame] | None:
    """None — библиотека скрыта или недоступна. Пустой список — игр нет."""
    response = payload.get("response")
    if not isinstance(response, dict):
        raise SteamQueryError("Steam вернул неожиданный ответ по библиотеке.")
    if "games" not in response and "game_count" not in response:
        return None
    result: list[OwnedGame] = []
    for item in response.get("games") or []:
        if not isinstance(item, dict) or "appid" not in item:
            continue
        try:
            appid = int(item["appid"])
            playtime = int(item.get("playtime_forever") or 0)
        except (TypeError, ValueError):
            continue
        name = clean_text(str(item.get("name") or f"App {appid}"), 200) or f"App {appid}"
        result.append(OwnedGame(appid=appid, name=name, playtime_forever=playtime))
    return result


def escape_md(value: str) -> str:
    """Снижает риск поломать вёрстку Discord и случайно кого-то пингануть."""
    text = value.replace("\\", "\\\\")
    for char in ("*", "_", "`", "|", "~", ">", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text.replace("@", "@\u200b")
