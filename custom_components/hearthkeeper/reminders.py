"""In-memory reminders for the Telegram chat.

A reminder is a one-shot point in time. It is deliberately kept in memory:
the text is user content, and the privacy promise says message texts never
touch disk. A restart therefore clears pending reminders, which the README
states explicitly.

The manager also creates a persistent notification when a reminder fires, so
it is visible in Home Assistant even if Telegram is closed.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

MAX_REMINDERS_PER_CHAT: Final = 20
MAX_DELAY: Final = timedelta(days=30)

_TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "title": "Hearthkeeper: напоминание",
        "fired": "⏰ Напоминание: {text}",
        "too_far": "Слишком далеко: напомню не позже чем через 30 дней.",
        "usage": (
            "Формат: /remind 20m текст, /remind 20:30 текст, "
            "или «напомни через 20 минут …»."
        ),
        "too_many": "Уже 20 напоминаний в этом чате. Дождитесь или отмените их.",
    },
    "en": {
        "title": "Hearthkeeper reminder",
        "fired": "⏰ Reminder: {text}",
        "too_far": "That is too far away: the limit is 30 days.",
        "usage": (
            "Format: /remind 20m text, /remind 20:30 text, "
            "or “remind me in 20 minutes to …”."
        ),
        "too_many": "This chat already has 20 reminders. Wait or cancel them.",
    },
}

_UNIT_SECONDS: Final[dict[str, int]] = {
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "мин": 60,
    "минута": 60,
    "минуту": 60,
    "минуты": 60,
    "минут": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "ч": 3600,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "д": 86400,
    "день": 86400,
    "дня": 86400,
    "дней": 86400,
}

_TRIGGER: Final[dict[str, re.Pattern[str]]] = {
    "ru": re.compile(r"^(?:напомни(?: мне)?|напоминание)\b[:,]?\s*", re.I),
    "en": re.compile(r"^(?:remind me|remind|reminder)\b[:,]?\s*", re.I),
}

_DELAY_PREFIX: Final[dict[str, re.Pattern[str]]] = {
    "ru": re.compile(r"^(?:через|спустя)\s+", re.I),
    "en": re.compile(r"^(?:in|after)\s+", re.I),
}

_DAY_PREFIX: Final[dict[str, re.Pattern[str]]] = {
    "ru": re.compile(r"^завтра\s+", re.I),
    "en": re.compile(r"^tomorrow\s+", re.I),
}

_CLOCK: Final[dict[str, re.Pattern[str]]] = {
    "ru": re.compile(r"^(?:в\s+)?(\d{1,2}):(\d{2})\b", re.I),
    "en": re.compile(r"^(?:at\s+)?(\d{1,2}):(\d{2})\b", re.I),
}

_TEXT_PREFIX: Final = re.compile(
    r"^(?:to|that|чтобы|что|и)\s+", re.I
)


@dataclass(frozen=True, slots=True)
class ReminderRequest:
    """A parsed "when" plus the text to repeat."""

    text: str
    due: datetime


@dataclass(slots=True)
class Reminder:
    """One scheduled reminder."""

    chat_id: int
    thread_id: int | None
    text: str
    due: datetime
    language: str
    cancel: Callable[[], None]


def _texts(language: str) -> dict[str, str]:
    return _TEXTS.get(language, _TEXTS["en"])


def looks_like_reminder(value: str, language: str) -> bool:
    """Tell whether a chat message starts with a reminder trigger."""
    trigger = _TRIGGER.get(language, _TRIGGER["en"])
    return trigger.match(value.strip()) is not None


def parse_reminder(
    value: str, language: str, now: datetime | None = None
) -> ReminderRequest | None:
    """Parse a chat phrase like "напомни через 20 минут …"."""
    trigger = _TRIGGER.get(language, _TRIGGER["en"])
    match = trigger.match(value.strip())
    if match is None:
        return None

    rest = value.strip()[match.end() :]
    return _parse_when_and_text(rest, language, now or dt_util.now())


def parse_reminder_command(
    value: str, language: str, now: datetime | None = None
) -> ReminderRequest | None:
    """Parse the arguments of ``/remind``."""
    return _parse_when_and_text(value.strip(), language, now or dt_util.now())


def _parse_when_and_text(
    value: str, language: str, now: datetime
) -> ReminderRequest | None:
    """Parse a delay or a clock time followed by the reminder text."""
    value = value.strip()
    if not value:
        return None

    tomorrow = False
    day_match = _DAY_PREFIX.get(language, _DAY_PREFIX["en"]).match(value)
    if day_match is not None:
        tomorrow = True
        value = value[day_match.end() :]

    delay_prefix = _DELAY_PREFIX.get(language, _DELAY_PREFIX["en"])
    if delay_prefix.match(value):
        value = value[delay_prefix.match(value).end() :]
        parsed = _take_duration(value)
        if parsed is None:
            return None
        delay, text = parsed
        if delay <= timedelta(0) or delay > MAX_DELAY:
            return None
        text = _clean_text(text)
        if not text:
            return None
        return ReminderRequest(text=text, due=now + delay)

    if tomorrow:
        clock = _CLOCK.get(language, _CLOCK["en"]).match(value)
        if clock is None:
            return None
        due = _clock_time(int(clock.group(1)), int(clock.group(2)), True, now)
        if due is None:
            return None
        text = _clean_text(value[clock.end() :])
        return ReminderRequest(text=text, due=due) if text else None

    clock = _CLOCK.get(language, _CLOCK["en"]).match(value)
    if clock is not None:
        due = _clock_time(int(clock.group(1)), int(clock.group(2)), False, now)
        if due is None:
            return None
        text = _clean_text(value[clock.end() :])
        return ReminderRequest(text=text, due=due) if text else None

    # The command form may start with a bare duration: "/remind 20m ...".
    parsed = _take_duration(value)
    if parsed is None:
        return None
    delay, text = parsed
    if delay <= timedelta(0) or delay > MAX_DELAY:
        return None
    text = _clean_text(text)
    return ReminderRequest(text=text, due=now + delay) if text else None


def _take_duration(value: str) -> tuple[timedelta, str] | None:
    """Read leading duration tokens such as ``20m`` or ``1 час 30 минут``."""
    total = 0
    rest = value.strip()
    found = False
    token = re.compile(r"^(\d+)\s*([A-Za-zА-Яа-я]+)")
    while (match := token.match(rest)) is not None:
        unit = match.group(2).lower()
        seconds = _UNIT_SECONDS.get(unit)
        if seconds is None:
            break
        total += int(match.group(1)) * seconds
        rest = rest[match.end() :].strip()
        found = True
    if not found or total <= 0:
        return None
    return timedelta(seconds=total), rest


def _clock_time(
    hour: int, minute: int, tomorrow: bool, now: datetime
) -> datetime | None:
    """Build a local datetime; a past time today moves to tomorrow."""
    if hour > 23 or minute > 59:
        return None
    day = dt_util.start_of_local_day(now) + timedelta(days=1 if tomorrow else 0)
    due = day.replace(hour=hour, minute=minute)
    if not tomorrow and due <= now:
        due += timedelta(days=1)
    return due


def _clean_text(value: str) -> str:
    """Strip a leading "to"/"что" and surrounding spaces."""
    text = value.strip()
    while (match := _TEXT_PREFIX.match(text)) is not None:
        text = text[match.end() :].strip()
    return text


class ReminderManager:
    """Schedule one-shot reminders and deliver them through a callback."""

    def __init__(
        self,
        hass: HomeAssistant,
        deliver: Callable[[int, int | None, str], Awaitable[None]],
    ) -> None:
        self.hass = hass
        self._deliver = deliver
        self._items: list[Reminder] = []

    @callback
    def async_add(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        text: str,
        due: datetime,
        language: str,
    ) -> str | None:
        """Schedule a reminder; return an error text when it cannot."""
        texts = _texts(language)
        if sum(1 for item in self._items if item.chat_id == chat_id) >= (
            MAX_REMINDERS_PER_CHAT
        ):
            return texts["too_many"]
        if due <= dt_util.now() or due - dt_util.now() > MAX_DELAY + timedelta(
            seconds=1
        ):
            return texts["too_far"]

        reminder = Reminder(
            chat_id=chat_id,
            thread_id=thread_id,
            text=text,
            due=due,
            language=language,
            cancel=lambda: None,
        )
        reminder.cancel = async_track_point_in_time(
            self.hass, partial(self._async_fire, reminder), due
        )
        self._items.append(reminder)
        return None

    async def _async_fire(self, reminder: Reminder, _now: datetime) -> None:
        """Deliver a due reminder and forget it."""
        if reminder not in self._items:
            return
        self._items.remove(reminder)
        await self._async_deliver(reminder)

    async def _async_deliver(self, reminder: Reminder) -> None:
        texts = _texts(reminder.language)
        message = texts["fired"].format(text=reminder.text)
        try:
            await self._deliver(reminder.chat_id, reminder.thread_id, message)
        except Exception as err:
            _LOGGER.debug("Could not deliver a reminder (%s)", type(err).__name__)
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": texts["title"],
                    "message": reminder.text,
                    "notification_id": f"hearthkeeper_reminder_{uuid.uuid4().hex}",
                },
                blocking=False,
            )
        except Exception as err:
            _LOGGER.debug("Could not create a reminder notification (%s)", err)

    def items(self, chat_id: int) -> list[Reminder]:
        """Return active reminders of one chat, soonest first."""
        return sorted(
            (item for item in self._items if item.chat_id == chat_id),
            key=lambda item: item.due,
        )

    def cancel_all(self, chat_id: int) -> int:
        """Cancel every reminder of a chat and return how many were removed."""
        removed = [item for item in self._items if item.chat_id == chat_id]
        for item in removed:
            self._items.remove(item)
            item.cancel()
        return len(removed)

    @callback
    def async_stop(self) -> None:
        """Cancel timers when the config entry unloads."""
        for item in self._items:
            item.cancel()
        self._items.clear()
