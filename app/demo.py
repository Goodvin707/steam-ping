from __future__ import annotations

DEMO_STEAM_ID = "demo-alex"
DEMO_NAME = "Алекс"
DEMO_AVATAR = "/static/demo-avatar.svg"
DEMO_PROFILE = ""

# Короткий запасной текст, если магазин Steam в момент клика не ответил.
DEMO_CATALOG: tuple[tuple[int, str, str], ...] = (
    (1145350, "Hades II", "Забег из подземного мира в новой части от Supergiant."),
    (2379780, "Balatro", "Покерные комбинации, собранные как колода в рогалике."),
    (1030300, "Hollow Knight: Silksong", "История Хорнет после Hollow Knight."),
    (413150, "Stardew Valley", "Ферма, соседи и сезон, который всегда кончается слишком быстро."),
    (1245620, "ELDEN RING", "Открытые земли и боссы, которые не представляются."),
    (292030, "The Witcher 3: Wild Hunt", "Геральт, заказы и дороги, которые длиннее основного сюжета."),
)


async def ensure_demo_friend(db) -> dict:
    return await db.upsert_friend(
        steam_id=DEMO_STEAM_ID,
        persona_name=DEMO_NAME,
        avatar_url=DEMO_AVATAR,
        profile_url=DEMO_PROFILE,
        is_demo=True,
    )
