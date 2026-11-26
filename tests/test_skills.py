"""Tests for the built-in skills and their intent entry points."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import intent as intent_helper
from homeassistant.util import dt as dt_util

from custom_components.hearthkeeper.brief import BriefingSources
from custom_components.hearthkeeper.skills import (
    DAY_TODAY,
    DAY_TOMORROW,
    INTENT_BRIEF,
    INTENT_CALENDAR,
    INTENT_HOME,
    INTENT_MAIL,
    SENTENCES_FILE,
    SkillRequest,
    SkillRouter,
    async_install_sentences,
    async_register_intents,
    async_reload_assist,
    async_remove_intents,
    async_remove_sentences,
    normalize,
)


@pytest.mark.parametrize(
    ("text", "intent", "day"),
    [
        ("Сводка!", INTENT_BRIEF, ""),
        ("брифинг на сегодня", INTENT_BRIEF, ""),
        ("что у меня сегодня", INTENT_CALENDAR, DAY_TODAY),
        ("какие планы на сегодня", INTENT_CALENDAR, DAY_TODAY),
        ("какие дела на завтра", INTENT_CALENDAR, DAY_TOMORROW),
        ("что у меня завтра", INTENT_CALENDAR, DAY_TOMORROW),
        ("Что дома?", INTENT_HOME, ""),
        ("статус дома", INTENT_HOME, ""),
        ("что в почте", INTENT_MAIL, ""),
        ("что нового в почте", INTENT_MAIL, ""),
        ("проверь почту", INTENT_MAIL, ""),
    ],
)
def test_match_russian_phrases(text: str, intent: str, day: str) -> None:
    """Short Russian phrases are routed without the agent."""
    request = SkillRouter.match(text, "ru")

    assert request == SkillRequest(intent, day)


@pytest.mark.parametrize(
    ("text", "intent", "day"),
    [
        ("Briefing", INTENT_BRIEF, ""),
        ("daily summary", INTENT_BRIEF, ""),
        ("What's on today?", INTENT_CALENDAR, DAY_TODAY),
        ("what do i have tomorrow", INTENT_CALENDAR, DAY_TOMORROW),
        ("tomorrow's agenda", INTENT_CALENDAR, DAY_TOMORROW),
        ("what is on at home", INTENT_HOME, ""),
        ("status of the home", INTENT_HOME, ""),
        ("what is in my mail", INTENT_MAIL, ""),
        ("check my email", INTENT_MAIL, ""),
    ],
)
def test_match_english_phrases(text: str, intent: str, day: str) -> None:
    """English phrases follow the same router."""
    request = SkillRouter.match(text, "en")

    assert request == SkillRequest(intent, day)


@pytest.mark.parametrize(
    "text",
    [
        "включи свет на кухне",
        "какая погода завтра",
        "что нового в мире",
        "turn on the kitchen light",
        "what is the weather tomorrow",
        "tell me a joke",
        "",
    ],
)
def test_unrelated_text_goes_to_the_agent(text: str) -> None:
    """General questions are not hijacked by the skill router."""
    assert SkillRouter.match(text, "ru") is None
    assert SkillRouter.match(text, "en") is None


def test_normalize() -> None:
    """Phrases are compared case- and punctuation-insensitively."""
    assert normalize("  Что   ДОМА?! ") == "что дома"
    assert normalize("What's on today?") == "whats on today"
    assert normalize("Планы на сегодня.") == "планы на сегодня"


async def test_calendar_skill_lists_one_day(hass: HomeAssistant) -> None:
    """The calendar skill only returns the requested day."""
    now = dt_util.now().replace(hour=9, minute=30, second=0, microsecond=0)
    tomorrow = now + timedelta(days=1)

    async def _events(call: Any) -> dict[str, Any]:
        return {
            "calendar.inbox": {
                "events": [
                    {"summary": "Встреча", "start": now.isoformat()},
                    {"summary": "Отпуск", "start": now.date().isoformat()},
                    {"summary": "Завтрашнее", "start": tomorrow.isoformat()},
                ]
            }
        }

    hass.services.async_register(
        "calendar", "get_events", _events, supports_response=SupportsResponse.ONLY
    )
    router = SkillRouter(hass, BriefingSources(calendars=("calendar.inbox",)))

    today = await router.async_run(SkillRequest(INTENT_CALENDAR, DAY_TODAY), "ru")
    upcoming = await router.async_run(
        SkillRequest(INTENT_CALENDAR, DAY_TOMORROW), "ru"
    )

    assert today.startswith("📅 Сегодня")
    assert "Встреча" in today
    assert "Отпуск" in today
    assert "Завтрашнее" not in today
    assert upcoming.startswith("📅 Завтра")
    assert "Завтрашнее" in upcoming
    assert "Встреча" not in upcoming


async def test_calendar_skill_without_sources(hass: HomeAssistant) -> None:
    """Without calendars the skill explains what to configure."""
    router = SkillRouter(hass, BriefingSources())

    answer = await router.async_run(SkillRequest(INTENT_CALENDAR, DAY_TODAY), "en")

    assert answer == "No calendars are configured in Hearthkeeper."


async def test_home_skill(hass: HomeAssistant) -> None:
    """The home skill reuses the briefing section."""
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Кухня"})
    hass.states.async_set(
        "binary_sensor.door",
        "on",
        {"device_class": "door", "friendly_name": "Входная дверь"},
    )
    router = SkillRouter(hass, BriefingSources(home=True))

    answer = await router.async_run(SkillRequest(INTENT_HOME), "ru")

    assert "🏠 Дом" in answer
    assert "свет включён: Кухня" in answer
    assert "Входная дверь" in answer


async def test_home_skill_disabled(hass: HomeAssistant) -> None:
    """A disabled home section is reported instead of a stack trace."""
    router = SkillRouter(hass, BriefingSources(home=False))

    answer = await router.async_run(SkillRequest(INTENT_HOME), "ru")

    assert "отключена" in answer


async def test_mail_skill_without_sources(hass: HomeAssistant) -> None:
    """Without mail sensors the skill explains what to configure."""
    router = SkillRouter(hass, BriefingSources())

    answer = await router.async_run(SkillRequest(INTENT_MAIL), "ru")

    assert "Сенсоры почты не выбраны" in answer


async def test_brief_skill(hass: HomeAssistant) -> None:
    """The brief skill returns the full briefing."""
    router = SkillRouter(hass, BriefingSources(home=False))

    answer = await router.async_run(SkillRequest(INTENT_BRIEF), "ru")

    assert "Сегодня" in answer


async def test_intent_handlers(hass: HomeAssistant) -> None:
    """Registered intents answer through the same router."""
    router = SkillRouter(hass, BriefingSources(home=False))
    async_register_intents(hass, router)
    try:
        response = await intent_helper.async_handle(
            hass,
            "conversation",
            INTENT_CALENDAR,
            {"day": {"value": DAY_TOMORROW}},
            text_input="what is on tomorrow",
            language="en",
        )
        home = await intent_helper.async_handle(
            hass,
            "conversation",
            INTENT_HOME,
            {},
            text_input="home status",
            language="en",
        )
    finally:
        async_remove_intents(hass)

    assert response.speech["plain"]["speech"] == (
        "No calendars are configured in Hearthkeeper."
    )
    assert "disabled" in home.speech["plain"]["speech"]


async def test_sentences_are_installed_and_removed(hass: HomeAssistant) -> None:
    """Sentence files carry our intents and are cleaned up on unload."""
    await async_install_sentences(hass)

    paths = [
        Path(hass.config.path("custom_sentences", language, SENTENCES_FILE))
        for language in ("ru", "en")
    ]
    for path in paths:
        assert path.is_file()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert payload["language"] in ("ru", "en")
        assert INTENT_BRIEF in payload["intents"]
        assert INTENT_CALENDAR in payload["intents"]
        assert INTENT_HOME in payload["intents"]
        assert INTENT_MAIL in payload["intents"]

    await async_remove_sentences(hass)

    assert all(not path.exists() for path in paths)


async def test_reload_assist_without_conversation(hass: HomeAssistant) -> None:
    """The reload helper stays silent when Assist is not set up."""
    await async_reload_assist(hass)


async def test_foreign_sentences_file_is_not_overwritten(hass: HomeAssistant) -> None:
    """A user file with the same name is left untouched."""
    path = Path(hass.config.path("custom_sentences", "ru", SENTENCES_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("language: ru\nintents: {}\n", encoding="utf-8")

    try:
        await async_install_sentences(hass)
        assert path.read_text(encoding="utf-8") == "language: ru\nintents: {}\n"
    finally:
        path.unlink(missing_ok=True)


async def test_foreign_sentences_file_is_kept(hass: HomeAssistant) -> None:
    """A user file with the same name is never deleted."""
    path = Path(hass.config.path("custom_sentences", "ru", SENTENCES_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("language: ru\nintents: {}\n", encoding="utf-8")

    try:
        await async_remove_sentences(hass)
        assert path.is_file()
    finally:
        path.unlink(missing_ok=True)
