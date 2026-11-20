"""Tests for the daily briefing."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.hearthkeeper.brief import (
    BriefingSources,
    async_build_briefing,
    parse_brief_time,
)


def test_parse_brief_time() -> None:
    """The options value is a time selector string."""
    assert parse_brief_time("07:30") == (7, 30)
    assert parse_brief_time("7:05:00") == (7, 5)
    assert parse_brief_time("23:59") == (23, 59)


def test_parse_brief_time_falls_back() -> None:
    """Broken or missing values keep the default morning time."""
    assert parse_brief_time(None) == (8, 0)
    assert parse_brief_time("") == (8, 0)
    assert parse_brief_time("25:00") == (8, 0)
    assert parse_brief_time("nope") == (8, 0)


async def test_empty_briefing_is_only_the_date(hass: HomeAssistant) -> None:
    """With no sources the briefing is still a valid greeting."""
    text = await async_build_briefing(hass, BriefingSources(home=False), "ru")

    assert "Сегодня" in text
    assert "📅" not in text
    assert "🏠" not in text


async def test_full_briefing(hass: HomeAssistant) -> None:
    """Every configured section shows up in one message."""
    now = dt_util.now()
    today = now.replace(hour=9, minute=30, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    async def _events(call: Any) -> dict[str, Any]:
        return {
            "calendar.inbox": {
                "events": [
                    {
                        "summary": "Встреча",
                        "start": today.isoformat(),
                        "location": "Офис",
                    },
                    {"summary": "Отпуск", "start": today.date().isoformat()},
                    {"summary": "Завтрашнее", "start": tomorrow.isoformat()},
                ]
            }
        }

    async def _items(call: Any) -> dict[str, Any]:
        return {"todo.inbox": {"items": [{"summary": "Молоко", "uid": "a"}]}}

    hass.services.async_register(
        "calendar", "get_events", _events, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        "todo", "get_items", _items, supports_response=SupportsResponse.ONLY
    )

    hass.states.async_set(
        "weather.home",
        "cloudy",
        {"temperature": 3.4, "apparent_temperature": 1.0},
    )
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Кухня"})
    hass.states.async_set(
        "binary_sensor.window",
        "on",
        {"device_class": "window", "friendly_name": "Окно"},
    )
    hass.states.async_set(
        "climate.living",
        "heat",
        {"friendly_name": "Гостиная", "current_temperature": 22.4},
    )
    hass.states.async_set(
        "sensor.mail", "3", {"friendly_name": "IMAP (me@example.com)"}
    )

    sources = BriefingSources(
        calendars=("calendar.inbox",),
        weather="weather.home",
        todo="todo.inbox",
        mail=("sensor.mail",),
        home=True,
    )
    text = await async_build_briefing(hass, sources, "ru")

    assert "☀️ Погода: облачно, +3 °C, ощущается как +1 °C" in text
    assert "• 09:30 — Встреча" in text
    assert "• весь день — Отпуск" in text
    assert "Завтра:" in text
    assert "Завтрашнее" in text
    assert "свет включён: 1" in text
    assert "Окно" in text
    assert "Гостиная +22 °C" in text
    assert "• Молоко" in text
    assert "IMAP (me@example.com): 3" in text


async def test_broken_calendar_does_not_break_the_briefing(
    hass: HomeAssistant,
) -> None:
    """A failing skill only replaces its own section."""

    async def _fail(call: Any) -> dict[str, Any]:
        raise HomeAssistantError("calendar is down")

    hass.services.async_register(
        "calendar", "get_events", _fail, supports_response=SupportsResponse.ONLY
    )
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Кухня"})

    text = await async_build_briefing(
        hass,
        BriefingSources(calendars=("calendar.inbox",), home=True),
        "ru",
    )

    assert "📅 Календарь: недоступно" in text
    assert "свет включён: 1" in text


async def test_quiet_home_and_empty_mail(hass: HomeAssistant) -> None:
    """An idle house and a zero unread counter get short answers."""
    hass.states.async_set("sensor.mail", "0", {"friendly_name": "Почта"})

    text = await async_build_briefing(
        hass,
        BriefingSources(mail=("sensor.mail",), home=True),
        "ru",
    )

    assert "всё спокойно" in text
    assert "новых писем нет" in text


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_unknown_weather_is_localised(hass: HomeAssistant, language: str) -> None:
    """Unknown conditions still render without a KeyError."""
    hass.states.async_set("weather.home", "something-new", {})

    text = await async_build_briefing(
        hass, BriefingSources(weather="weather.home", home=False), language
    )

    assert "something-new" in text


def test_quiet_hours_accept_seconds() -> None:
    """The options flow stores HH:MM:SS; quiet hours must still work."""
    from datetime import datetime

    from custom_components.hearthkeeper.brief import is_quiet_hours

    night = datetime(2026, 11, 20, 23, 30)
    noon = datetime(2026, 11, 20, 12, 0)
    assert is_quiet_hours(night, "23:00:00", "07:00:00")
    assert not is_quiet_hours(noon, "23:00:00", "07:00:00")
