"""Tests for personal settings changed from the Mini App."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.hearthkeeper.settings import (
    InvalidPreference,
    apply_preferences,
)
from custom_components.hearthkeeper.store import (
    BriefingPlan,
    ChatContext,
    CustomSkill,
    normalise_time,
)

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
            "briefings": [
                {"kind": "morning", "time": "06:30", "enabled": True, "name": ""},
                {"kind": "evening", "time": "20:00", "enabled": True, "name": "Вечер"},
            ],
            "quiet": ["22:00", "06:00"],
            "weather": "weather.dacha",
            "calendars": ["calendar.work", "calendar.work"],
            "mail": [],
            "home": False,
        },
        **POOLS,
    )

    assert sorted(changed) == [
        "briefings",
        "calendars",
        "home",
        "mail",
        "quiet",
        "weather",
    ]
    assert chat.briefings is not None
    assert len(chat.briefings) == 2
    assert chat.briefings[0].kind == "morning"
    assert chat.briefings[0].time == "06:30"
    assert chat.briefings[1].kind == "evening"
    assert chat.briefings[1].time == "20:00"
    assert chat.briefings[1].name == "Вечер"
    assert chat.quiet == ["22:00", "06:00"]
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
        {"briefings": "yes"},
        {"briefings": [{"kind": "morning", "time": "bad"}]},
        {"briefings": [{"time": "08:00"}]},
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

    with pytest.raises(InvalidPreference):
        apply_preferences(chat, patch or {}, **POOLS)

    assert not chat.has_prefs


def test_preferences_survive_storage() -> None:
    """Personal settings are written and read back with the chat."""
    chat = _chat()
    apply_preferences(
        chat,
        {
            "briefings": [{"kind": "evening", "time": "19:30", "enabled": False}],
            "quiet": [],
            "calendars": ["calendar.family"],
        },
        **POOLS,
    )

    restored = ChatContext.from_dict(42, chat.as_dict())

    assert restored.briefings is not None
    assert len(restored.briefings) == 1
    assert restored.briefings[0].kind == "evening"
    assert restored.briefings[0].time == "19:30"
    assert restored.briefings[0].enabled is False
    assert restored.quiet == []
    assert restored.sources == {"calendars": ["calendar.family"]}

def test_custom_skills_are_validated() -> None:
    """Skills are stored trimmed; the page cannot forge ``last_run``."""
    chat = _chat()
    chat.custom_skills = [
        CustomSkill(
            id="a1", name="Old", prompt="Old", last_run="2026-11-20T08:00:00+00:00"
        )
    ]

    apply_preferences(
        chat,
        {
            "skills": [
                {
                    "id": "a1",
                    "name": " Шутка ",
                    "prompt": " Расскажи шутку ",
                    "time": "09:00:00",
                    "last_run": "x",
                },
                {
                    "name": "Ужин",
                    "prompt": "Что приготовить?",
                    "time": "",
                },
            ]
        },
        **POOLS,
    )

    first, second = chat.custom_skills
    assert (first.id, first.name, first.prompt, first.time) == (
        "a1",
        "Шутка",
        "Расскажи шутку",
        "09:00",
    )
    assert first.last_run == "2026-11-20T08:00:00+00:00"
    assert second.time == "" and second.id
    assert second.last_run == ""


@pytest.mark.parametrize(
    "skills",
    [
        "nope",
        [{"name": "", "prompt": "x"}],
        [{"name": "x", "prompt": ""}],
        [{"name": "x" * 41, "prompt": "x"}],
        [{"name": "x", "prompt": "x" * 601}],
        [{"name": "x", "prompt": "x", "time": "25:00"}],
        [{"name": "x", "prompt": "x", "enabled": "yes"}],
        [
            {"id": "a", "name": "x", "prompt": "x"},
            {"id": "a", "name": "y", "prompt": "y"},
        ],
        [{"name": "x", "prompt": "x"}] * 21,
        [{"name": "x", "prompt": "x", "days": [7]}],
        [{"name": "x", "prompt": "x", "days": [True]}],
        [{"name": "x", "prompt": "x", "days": "mon"}],
    ],
)
def test_bad_custom_skills_change_nothing(skills: Any) -> None:
    """A bad skill list is refused as a whole."""
    chat = _chat()
    with pytest.raises(InvalidPreference):
        apply_preferences(chat, {"skills": skills}, **POOLS)
    assert chat.custom_skills == []


def test_reset_keeps_custom_skills() -> None:
    """Resetting the household overrides does not delete the user's skills."""
    chat = _chat()
    apply_preferences(chat, {"skills": [{"name": "x", "prompt": "y"}]}, **POOLS)
    apply_preferences(chat, {"reset": True}, **POOLS)
    assert len(chat.custom_skills) == 1


def test_custom_skills_survive_storage() -> None:
    """Skills round-trip through the stored dict."""
    chat = _chat()
    skills = [{"name": "x", "prompt": "y", "time": "07:00"}]
    apply_preferences(chat, {"skills": skills}, **POOLS)
    restored = ChatContext.from_dict(42, chat.as_dict())
    assert restored.custom_skills == chat.custom_skills


def test_custom_skill_days() -> None:
    """Weekdays are sorted and deduplicated; all seven mean every day."""
    chat = _chat()
    skills = [
        {"name": "a", "prompt": "x", "days": [4, 0, 4]},
        {"name": "b", "prompt": "x", "days": list(range(7))},
    ]
    apply_preferences(chat, {"skills": skills}, **POOLS)

    weekdays, every_day = chat.custom_skills
    assert weekdays.days == [0, 4]
    assert weekdays.runs_on(0) and not weekdays.runs_on(1)
    assert every_day.days == [] and every_day.runs_on(6)


def test_calendar_alerts_and_mascot() -> None:
    """Event reminders, birthdays and the mascot are validated and stored."""
    chat = _chat()
    apply_preferences(
        chat,
        {
            "event_reminder": 15,
            "birthdays": "09:00:00",
            "mascot": {"color": "mint", "name": " Уголёк "},
        },
        **POOLS,
    )
    assert (chat.event_reminder, chat.birthdays) == (15, "09:00")
    assert (chat.mascot_color, chat.mascot_name) == ("mint", "Уголёк")
    restored = ChatContext.from_dict(42, chat.as_dict())
    assert (restored.event_reminder, restored.birthdays, restored.mascot_color) == (
        15,
        "09:00",
        "mint",
    )

    apply_preferences(chat, {"birthdays": ""}, **POOLS)
    assert chat.birthdays == ""
    for bad in (
        {"event_reminder": 7},
        {"event_reminder": True},
        {"birthdays": "9am"},
        {"mascot": {"color": "plaid"}},
        {"mascot": {"name": "x" * 21}},
        {"mascot": "mint"},
    ):
        with pytest.raises(InvalidPreference):
            apply_preferences(chat, bad, **POOLS)
