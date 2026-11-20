"""Tests for personal settings changed from the Mini App."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.hearthkeeper.settings import (
    InvalidPreference,
    apply_preferences,
    normalise_time,
)
from custom_components.hearthkeeper.store import ChatContext

POOLS: dict[str, Any] = {
    "weather_ids": ["weather.home", "weather.dacha"],
    "calendar_pool": ["calendar.family", "calendar.work"],
    "mail_pool": ["sensor.inbox"],
}


def _chat() -> ChatContext:
    return ChatContext(chat_id=42, conversation_id="tg:42")


def test_normalise_time() -> None:
    """Times are stored as HH:MM whatever the page or options send."""
    assert normalise_time("07:05") == "07:05"
    assert normalise_time("23:00:00") == "23:00"
    for bad in ("7:05", "24:00", "12:60", "", None, 800):
        assert normalise_time(bad) is None


def test_preferences_are_stored_on_the_chat() -> None:
    """Valid values land on the chat, normalised."""
    chat = _chat()

    changed = apply_preferences(
        chat,
        {
            "brief_enabled": True,
            "brief_time": "06:30:00",
            "quiet": ["22:00", "06:00"],
            "weather": "weather.dacha",
            "calendars": ["calendar.work", "calendar.work"],
            "mail": [],
            "home": False,
        },
        **POOLS,
    )

    assert changed == [
        "brief_enabled",
        "brief_time",
        "calendars",
        "home",
        "mail",
        "quiet",
        "weather",
    ]
    assert (chat.brief_enabled, chat.brief_time, chat.quiet) == (
        True,
        "06:30",
        ["22:00", "06:00"],
    )
    assert chat.sources == {
        "weather": "weather.dacha",
        "calendars": ["calendar.work"],
        "mail": [],
        "home": False,
    }
    assert chat.has_prefs

    # Turning quiet hours off is a choice of its own, not a reset.
    apply_preferences(chat, {"quiet": []}, **POOLS)
    assert chat.quiet == []

    apply_preferences(chat, {"reset": True}, **POOLS)
    assert not chat.has_prefs


@pytest.mark.parametrize(
    "patch",
    [
        None,
        {},
        {"pipeline_id": "x"},
        {"brief_enabled": "yes"},
        {"brief_time": "8am"},
        {"quiet": ["22:00"]},
        {"weather": "weather.unknown"},
        # Private data outside what the integration options share.
        {"calendars": ["calendar.boss"]},
        {"mail": ["sensor.someone_else"]},
        {"mail": "sensor.inbox"},
        {"home": 1},
        {"reset": False},
    ],
)
def test_bad_preferences_change_nothing(patch: Any) -> None:
    """One bad value refuses the whole patch."""
    chat = _chat()
    bad = dict(patch or {})
    if bad:
        bad = {"brief_time": "07:00", **bad}

    with pytest.raises(InvalidPreference):
        apply_preferences(chat, bad or patch, **POOLS)

    assert not chat.has_prefs


def test_preferences_survive_storage() -> None:
    """Personal settings are written and read back with the chat."""
    chat = _chat()
    apply_preferences(
        chat,
        {"brief_enabled": False, "quiet": [], "calendars": ["calendar.family"]},
        **POOLS,
    )

    restored = ChatContext.from_dict(42, chat.as_dict())

    assert restored.brief_enabled is False
    assert restored.quiet == []
    assert restored.sources == {"calendars": ["calendar.family"]}
