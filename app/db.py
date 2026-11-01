from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS friends (
    steam_id TEXT PRIMARY KEY,
    persona_name TEXT NOT NULL,
    avatar_url TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    baseline_done INTEGER NOT NULL DEFAULT 0,
    game_count INTEGER NOT NULL DEFAULT 0,
    last_checked_at INTEGER,
    last_error TEXT,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS owned_games (
    steam_id TEXT NOT NULL,
    appid INTEGER NOT NULL,
    name TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    PRIMARY KEY (steam_id, appid)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    steam_id TEXT NOT NULL,
    persona_name TEXT NOT NULL,
    avatar_url TEXT NOT NULL DEFAULT '',
    appid INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    header_image TEXT NOT NULL DEFAULT '',
    short_description TEXT NOT NULL DEFAULT '',
    price_text TEXT NOT NULL DEFAULT '',
    store_url TEXT NOT NULL DEFAULT '',
    detected_at INTEGER NOT NULL,
    discord_sent INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'game',
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_detected ON events (detected_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("База ещё не открыта")
        return self._db

    async def get_setting(self, key: str) -> str | None:
        cursor = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return None if row is None else str(row["value"])

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self.conn.commit()

    async def delete_setting(self, key: str) -> None:
        await self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self.conn.commit()

    async def upsert_friend(
        self,
        *,
        steam_id: str,
        persona_name: str,
        avatar_url: str,
        profile_url: str,
        is_demo: bool = False,
    ) -> dict:
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO friends (
                steam_id, persona_name, avatar_url, profile_url, is_demo, added_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(steam_id) DO UPDATE SET
                persona_name = excluded.persona_name,
                avatar_url = excluded.avatar_url,
                profile_url = excluded.profile_url
            """,
            (steam_id, persona_name, avatar_url, profile_url, int(is_demo), now),
        )
        await self.conn.commit()
        friend = await self.get_friend(steam_id)
        assert friend is not None
        return friend

    async def list_friends(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM friends ORDER BY added_at ASC, steam_id ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_friend(self, steam_id: str) -> dict | None:
        cursor = await self.conn.execute(
            "SELECT * FROM friends WHERE steam_id = ?",
            (steam_id,),
        )
        return _row(await cursor.fetchone())

    async def delete_friend(self, steam_id: str) -> bool:
        await self.conn.execute("DELETE FROM owned_games WHERE steam_id = ?", (steam_id,))
        cursor = await self.conn.execute(
            "DELETE FROM friends WHERE steam_id = ?",
            (steam_id,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def known_appids(self, steam_id: str) -> set[int]:
        cursor = await self.conn.execute(
            "SELECT appid FROM owned_games WHERE steam_id = ?",
            (steam_id,),
        )
        rows = await cursor.fetchall()
        return {int(row["appid"]) for row in rows}

    async def add_owned(self, steam_id: str, games: list) -> None:
        now = int(time.time())
        await self.conn.executemany(
            """
            INSERT INTO owned_games (steam_id, appid, name, first_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(steam_id, appid) DO UPDATE SET name = excluded.name
            """,
            [(steam_id, game.appid, game.name, now) for game in games],
        )
        await self.conn.commit()

    async def set_check(
        self,
        steam_id: str,
        *,
        game_count: int | None = None,
        baseline: bool = False,
        error: str | None = None,
    ) -> None:
        friend = await self.get_friend(steam_id)
        if friend is None:
            return
        count = friend["game_count"] if game_count is None else game_count
        await self.conn.execute(
            """
            UPDATE friends
            SET game_count = ?,
                last_checked_at = ?,
                last_error = ?,
                baseline_done = CASE WHEN ? = 1 THEN 1 ELSE baseline_done END
            WHERE steam_id = ?
            """,
            (count, int(time.time()), error, int(baseline), steam_id),
        )
        await self.conn.commit()

    async def add_event(
        self,
        *,
        steam_id: str,
        persona_name: str,
        avatar_url: str,
        appid: int,
        name: str,
        header_image: str = "",
        short_description: str = "",
        price_text: str = "",
        store_url: str = "",
        kind: str = "game",
        is_demo: bool = False,
        discord_sent: int = 0,
    ) -> dict:
        now = int(time.time())
        cursor = await self.conn.execute(
            """
            INSERT INTO events (
                steam_id, persona_name, avatar_url, appid, name, header_image,
                short_description, price_text, store_url, detected_at,
                discord_sent, kind, is_demo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                steam_id,
                persona_name,
                avatar_url,
                appid,
                name,
                header_image,
                short_description,
                price_text,
                store_url,
                now,
                discord_sent,
                kind,
                int(is_demo),
            ),
        )
        await self.conn.commit()
        event = await self.get_event(int(cursor.lastrowid))
        assert event is not None
        return event

    async def get_event(self, event_id: int) -> dict | None:
        cursor = await self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        return _row(await cursor.fetchone())

    async def list_events(self, limit: int = 40) -> list[dict]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM events
            ORDER BY detected_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def count_events_for(self, steam_id: str, *, kind: str = "game") -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE steam_id = ? AND kind = ?",
            (steam_id, kind),
        )
        row = await cursor.fetchone()
        return int(row["n"])

    async def count_notes_with_name(self, steam_id: str, name: str) -> int:
        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM events
            WHERE steam_id = ? AND kind = 'note' AND name = ?
            """,
            (steam_id, name),
        )
        row = await cursor.fetchone()
        return int(row["n"])

    async def pending_game_events(self, limit: int = 20) -> list[dict]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM events
            WHERE kind = 'game' AND is_demo = 0 AND discord_sent = 0
            ORDER BY detected_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_events(self, event_ids: list[int], discord_sent: int) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        await self.conn.execute(
            f"UPDATE events SET discord_sent = ? WHERE id IN ({placeholders})",
            (discord_sent, *event_ids),
        )
        await self.conn.commit()
