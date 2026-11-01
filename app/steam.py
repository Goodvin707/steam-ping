from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.logic import OwnedGame, SteamQueryError, clean_text, parse_owned_payload, parse_steam_query

log = logging.getLogger(__name__)

API = "https://api.steampowered.com"
STORE = "https://store.steampowered.com/api/appdetails"
PRIVATE_MESSAGE = (
    "Библиотека этого профиля скрыта. Пока список игр приватный, новые игры не видны."
)


class SteamError(Exception):
    pass


@dataclass(frozen=True)
class Player:
    steam_id: str
    persona_name: str
    avatar_url: str
    profile_url: str


@dataclass(frozen=True)
class AppDetails:
    appid: int
    type: str
    name: str
    short_description: str
    header_image: str
    is_free: bool
    price_text: str


def header_fallback(appid: int) -> str:
    return f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"


def store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}?l=russian"


class SteamClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient, *, store_delay: float = 0.35):
        self.api_key = api_key
        self.http = http
        self.store_delay = store_delay

    async def resolve(self, query: str) -> str:
        kind, value = parse_steam_query(query)
        if kind == "steamid":
            return value
        data = await self._api_get(
            "/ISteamUser/ResolveVanityURL/v1/",
            {"vanityurl": value},
        )
        response = data.get("response") or {}
        if response.get("success") != 1 or not response.get("steamid"):
            raise SteamError(f"Профиль Steam «{value}» не найден.")
        return str(response["steamid"])

    async def get_player(self, steam_id: str) -> Player:
        data = await self._api_get(
            "/ISteamUser/GetPlayerSummaries/v2/",
            {"steamids": steam_id},
        )
        players = (data.get("response") or {}).get("players") or []
        if not players:
            raise SteamError("Профиль Steam не найден.")
        player = players[0]
        return Player(
            steam_id=str(player.get("steamid") or steam_id),
            persona_name=clean_text(player.get("personaname"), 64) or steam_id,
            avatar_url=str(player.get("avatarfull") or ""),
            profile_url=str(
                player.get("profileurl")
                or f"https://steamcommunity.com/profiles/{steam_id}"
            ),
        )

    async def get_owned_games(self, steam_id: str) -> list[OwnedGame] | None:
        data = await self._api_get(
            "/IPlayerService/GetOwnedGames/v1/",
            {
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
        )
        try:
            return parse_owned_payload(data)
        except SteamQueryError as exc:
            raise SteamError(str(exc)) from exc

    async def get_app_details(self, appid: int) -> AppDetails | None:
        details = await fetch_app_details(self.http, appid)
        if self.store_delay:
            await asyncio.sleep(self.store_delay)
        return details

    async def _api_get(self, path: str, params: dict) -> dict:
        if not self.api_key:
            raise SteamError("Не задан STEAM_API_KEY.")
        query = {"key": self.api_key, "format": "json", **params}
        try:
            response = await self.http.get(API + path, params=query)
        except httpx.TimeoutException as exc:
            raise SteamError("Steam не ответил вовремя.") from exc
        except httpx.HTTPError as exc:
            raise SteamError("Не удалось связаться со Steam.") from exc
        if response.status_code in {401, 403}:
            raise SteamError("Ключ Steam API отклонён. Проверь STEAM_API_KEY.")
        if response.status_code == 429:
            raise SteamError("Steam просит подождать: слишком много запросов.")
        if response.status_code >= 400:
            raise SteamError(f"Steam ответил ошибкой {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise SteamError("Steam вернул не JSON.") from exc


async def fetch_app_details(http: httpx.AsyncClient, appid: int) -> AppDetails | None:
    try:
        response = await http.get(
            STORE,
            params={"appids": str(appid), "l": "russian", "cc": "ru"},
        )
    except httpx.HTTPError:
        log.info("Магазин Steam не ответил по appid %s", appid)
        return None
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    node = body.get(str(appid)) if isinstance(body, dict) else None
    if not isinstance(node, dict) or not node.get("success"):
        return None
    data = node.get("data") or {}
    if not isinstance(data, dict):
        return None
    price = ""
    if data.get("is_free"):
        price = "Бесплатно"
    else:
        overview = data.get("price_overview") or {}
        if isinstance(overview, dict):
            price = clean_text(str(overview.get("final_formatted") or ""), 40)
    header = str(data.get("header_image") or "") or header_fallback(appid)
    return AppDetails(
        appid=appid,
        type=str(data.get("type") or ""),
        name=clean_text(str(data.get("name") or ""), 200),
        short_description=clean_text(data.get("short_description"), 280),
        header_image=header,
        is_free=bool(data.get("is_free")),
        price_text=price,
    )
