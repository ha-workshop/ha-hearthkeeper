"""Tests for reminder parsing and scheduling."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hearthkeeper.reminders import (
    MAX_REMINDERS_PER_CHAT,
    ReminderManager,
    looks_like_reminder,
    parse_reminder,
    parse_reminder_command,
)


def test_parse_russian_phrase() -> None:
    """A natural phrase turns into a due time and text."""
    now = dt_util.now().replace(second=0, microsecond=0)

    request = parse_reminder("напомни через 20 минут выключить духовку", "ru", now)

    assert request is not None
    assert request.text == "выключить духовку"
    assert request.due == now + timedelta(minutes=20)


def test_parse_russian_clock_and_tomorrow() -> None:
    """Clock times move to tomorrow when today is already past."""
    now = dt_util.now().replace(hour=22, minute=0, second=0, microsecond=0)

    tonight = parse_reminder("напомни в 09:30 позвонить маме", "ru", now)
    tomorrow = parse_reminder("напомни завтра в 09:30 позвонить маме", "ru", now)

    assert tonight is not None
    assert tomorrow is not None
    assert tonight.text == "позвонить маме"
    assert tonight.due.date() == (now + timedelta(days=1)).date()
    assert tomorrow.due.date() == (now + timedelta(days=1)).date()
    assert tonight.due.hour == 9 and tonight.due.minute == 30


def test_parse_english_phrase() -> None:
    """English uses the same parser with its own trigger words."""
    now = dt_util.now().replace(second=0, microsecond=0)

    request = parse_reminder(
        "remind me in 1 hour 30 minutes to call the plumber", "en", now
    )

    assert request is not None
    assert request.text == "call the plumber"
    assert request.due == now + timedelta(hours=1, minutes=30)


def test_parse_command_formats() -> None:
    """``/remind`` accepts compact delays and clock times."""
    now = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0)

    compact = parse_reminder_command("1h30m выключить духовку", "ru", now)
    clock = parse_reminder_command("20:30 позвонить", "ru", now)

    assert compact is not None
    assert compact.text == "выключить духовку"
    assert compact.due == now + timedelta(hours=1, minutes=30)
    assert clock is not None
    assert clock.text == "позвонить"
    assert clock.due.hour == 20 and clock.due.minute == 30


@pytest.mark.parametrize(
    "value",
    [
        "",
        "20m",
        "напомни мне о встрече",
        "напомни через 0 минут ничего",
        "напомни через 400 дней слишком далеко",
        "напомни в 25:99 ничего",
        "remind me someday",
    ],
)
def test_parse_rejects_broken_input(value: str) -> None:
    """Broken or incomplete reminders are not scheduled."""
    assert parse_reminder(value, "ru") is None
    assert parse_reminder_command(value, "ru") is None


def test_looks_like_reminder() -> None:
    """Only messages with a trigger are handed to the reminder parser."""
    assert looks_like_reminder("напомни через 5 минут", "ru")
    assert looks_like_reminder("Remind me in 5 minutes", "en")
    assert not looks_like_reminder("включи свет", "ru")


async def test_manager_fires_and_delivers(hass: HomeAssistant) -> None:
    """A due reminder is delivered once and leaves the list."""
    delivered: list[tuple[int, int | None, str]] = []

    async def _deliver(chat_id: int, thread_id: int | None, text: str) -> None:
        delivered.append((chat_id, thread_id, text))

    manager = ReminderManager(hass, _deliver)
    due = dt_util.now() + timedelta(minutes=5)

    assert (
        manager.async_add(
            chat_id=42,
            thread_id=7,
            text="выключить духовку",
            due=due,
            language="ru",
        )
        is None
    )
    assert len(manager.items(42)) == 1

    async_fire_time_changed(hass, due + timedelta(seconds=1))
    await hass.async_block_till_done()
    manager.async_stop()

    assert delivered == [(42, 7, "⏰ Напоминание: выключить духовку")]
    assert manager.items(42) == []


async def test_manager_cancels_and_limits(hass: HomeAssistant) -> None:
    """Cancelling removes timers; a chat has a hard limit."""
    manager = ReminderManager(hass, _deliver_stub)
    due = dt_util.now() + timedelta(minutes=5)
    for _ in range(MAX_REMINDERS_PER_CHAT):
        assert (
            manager.async_add(
                chat_id=1, thread_id=None, text="x", due=due, language="en"
            )
            is None
        )

    assert "20 reminders" in (
        manager.async_add(
            chat_id=1, thread_id=None, text="x", due=due, language="en"
        )
        or ""
    )
    assert manager.cancel_all(1) == MAX_REMINDERS_PER_CHAT
    assert manager.items(1) == []
    manager.async_stop()


async def test_manager_rejects_too_far(hass: HomeAssistant) -> None:
    """Delays above the limit are refused with a localised message."""
    manager = ReminderManager(hass, _deliver_stub)

    error = manager.async_add(
        chat_id=1,
        thread_id=None,
        text="x",
        due=dt_util.now() + timedelta(days=31),
        language="ru",
    )

    assert error is not None and "30 дней" in error
    manager.async_stop()


async def _deliver_stub(chat_id: int, thread_id: int | None, text: str) -> None:
    """Do nothing on delivery."""
