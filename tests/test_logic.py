import base64

import pytest

from app.config import client_id_from_token, load_settings
from app.logic import (
    OwnedGame,
    classify_newcomers,
    clean_text,
    decide,
    escape_md,
    looks_truncated,
    parse_owned_payload,
    parse_steam_query,
)
from app.notify import game_message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("76561198000000000", ("steamid", "76561198000000000")),
        (
            "https://steamcommunity.com/profiles/76561198000000000/",
            ("steamid", "76561198000000000"),
        ),
        ("https://steamcommunity.com/id/gaben", ("vanity", "gaben")),
        ("https://steamcommunity.com/id/gaben/?l=russian", ("vanity", "gaben")),
        ("gaben", ("vanity", "gaben")),
    ],
)
def test_parse_steam_query(raw, expected):
    assert parse_steam_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "https://example.com/id/gaben", "a", "??"])
def test_parse_steam_query_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_steam_query(raw)


def test_clean_text_strips_tags_and_limits():
    assert clean_text("Раз&nbsp;два<br>три", 50) == "Раз два три"
    assert clean_text("абвгде", 4) == "абв…"


def test_escape_md_neutralizes_mentions():
    escaped = escape_md("@everyone *sale*")
    assert "@everyone" not in escaped
    assert "\\*" in escaped


def test_classify_and_truncation():
    assert classify_newcomers(100, 0) == "none"
    assert classify_newcomers(100, 2) == "each"
    assert classify_newcomers(100, 6) == "digest"
    assert classify_newcomers(100, 21) == "rebase"
    assert classify_newcomers(0, 12) == "rebase"
    assert classify_newcomers(0, 3) == "each"
    assert looks_truncated(40, 0) is True
    assert looks_truncated(40, 10) is True
    assert looks_truncated(40, 30) is False
    assert looks_truncated(0, 0) is False


def test_decide_baseline_private_and_new_game():
    known = {10, 20}
    owned = [OwnedGame(10, "Старая"), OwnedGame(30, "Новая")]
    assert decide(baseline_done=False, previous_count=0, known=set(), owned=owned).action == "baseline"
    assert decide(baseline_done=True, previous_count=2, known=known, owned=None).action == "private"
    decision = decide(baseline_done=True, previous_count=2, known=known, owned=owned)
    assert decision.action == "each"
    assert [game.appid for game in decision.newcomers] == [30]


def test_parse_owned_payload():
    assert parse_owned_payload({"response": {}}) is None
    assert parse_owned_payload({"response": {"game_count": 0}}) == []
    games = parse_owned_payload(
        {
            "response": {
                "game_count": 1,
                "games": [{"appid": 730, "name": "Counter-Strike 2", "playtime_forever": 5}],
            }
        }
    )
    assert games == [OwnedGame(730, "Counter-Strike 2", 5)]


def test_game_message_has_store_link_and_no_secret():
    content, embed = game_message(
        {
            "persona_name": "Алекс",
            "name": "Hades II",
            "short_description": "Забег.",
            "price_text": "999 руб.",
            "store_url": "https://store.steampowered.com/app/1145350",
            "header_image": "https://example.com/header.jpg",
            "avatar_url": "https://example.com/a.jpg",
        }
    )
    assert "Hades II" in content
    assert embed["url"].endswith("/1145350")
    assert embed["image"]["url"].endswith("header.jpg")
    assert "STEAM_API_KEY" not in str(embed)


def test_client_id_from_token():
    snowflake = "123456789012345678"
    encoded = base64.b64encode(snowflake.encode()).decode().rstrip("=")
    assert client_id_from_token(f"{encoded}.signature.hmac") == snowflake


def test_demo_mode_auto(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("STEAM_API_KEY", "")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://example.com/hook")
    settings = load_settings()
    assert settings.demo_mode is True
    assert settings.discord_webhook_url == ""
    assert settings.steam_enabled is False

    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("STEAM_API_KEY", "key")
    monkeypatch.setenv("DEMO_MODE", "auto")
    assert load_settings().demo_mode is False
