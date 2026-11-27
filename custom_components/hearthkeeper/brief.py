"""Daily briefing assembled from Home Assistant entities.

Every section is optional and independent: a broken calendar or a deleted
entity removes its own block and never the whole message. The content of
calendars and mail is logged at DEBUG level or lower, if at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    BRIEFING_EVENING,
    BRIEFING_MORNING,
    DEFAULT_BRIEF_TIME,
    DEFAULT_LANGUAGE,
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
)
from .mail import Mailbox, async_mail_digest

_LOGGER = logging.getLogger(__name__)

MAX_EVENTS = 6
MAX_TODO_ITEMS = 5
MAX_OPENINGS = 3
MAX_CLIMATES = 2
MAX_LIGHTS = 3

# Screens and indicator LEDs are exposed as ``light`` entities by some
# integrations, but they are not room lighting and only make the briefing
# count look wrong.
_TECHNICAL_LIGHT_SUFFIXES = ("_screen", "_status", "_backlight")
_TECHNICAL_LIGHT_WORDS = (
    "indicator",
    "индикатор",
    "screen",
    "экран",
    "backlight",
    "подсветка кнопок",
)

_OPENING_CLASSES = frozenset(("door", "window", "garage_door", "opening"))


@dataclass(frozen=True, slots=True)
class BriefingSources:
    """Entities the briefing reads, all chosen in the options flow."""

    calendars: tuple[str, ...] = ()
    weather: str | None = None
    todo: str | None = None
    mail: tuple[str, ...] = ()
    home: bool = True
    # Secret iCal links the user added in the Mini App; read only for them.
    ics: tuple[str, ...] = ()

    @property
    def has_calendars(self) -> bool:
        """Return True when any calendar, shared or personal, is set."""
        return bool(self.calendars or self.ics)


_TEXTS: dict[str, dict[str, Any]] = {
    "ru": {
        "morning": "Доброе утро!",
        "day": "Добрый день!",
        "evening": "Добрый вечер!",
        "date": "Сегодня {weekday}, {day} {month}.",
        "day_title": "🌅 Планы на день",
        "evening_title": "🌇 Вечерняя сводка",
        "day_summary": "Сегодня: {events} событий, {todo} задач, {lights} свет.",
        "weather": "☀️ Погода",
        "calendar": "📅 Календарь",
        "calendar_today": "📅 Сегодня",
        "calendar_tomorrow": "📅 Завтра",
        "home": "🏠 Дом",
        "todo": "🛒 Покупки",
        "mail": "✉️ Почта",
        "tomorrow": "Завтра:",
        "all_day": "весь день",
        "no_events": "событий нет",
        "no_todo": "список пуст",
        "no_mail": "новых писем нет",
        "all_quiet": "всё спокойно",
        "light": "свет включён: {items}",
        "lights": "свет включён: {count} — {items}",
        "openings": "открыто: {items}",
        "more": "… и ещё {count}",
        "unavailable": "{section}: недоступно",
        "feels_like": "ощущается как {value}",
        "weekdays": (
            "понедельник",
            "вторник",
            "среда",
            "четверг",
            "пятница",
            "суббота",
            "воскресенье",
        ),
        "months": (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        "weather_states": {
            "clear-night": "ясно",
            "cloudy": "облачно",
            "exceptional": "аномальная погода",
            "fog": "туман",
            "hail": "град",
            "lightning": "гроза",
            "lightning-rainy": "гроза с дождём",
            "partlycloudy": "переменная облачность",
            "pouring": "ливень",
            "rainy": "дождь",
            "snowy": "снег",
            "snowy-rainy": "снег с дождём",
            "sunny": "ясно",
            "windy": "ветрено",
            "windy-variant": "ветрено, облачно",
        },
    },
    "en": {
        "morning": "Good morning!",
        "day": "Good afternoon!",
        "evening": "Good evening!",
        "date": "Today is {weekday}, {day} {month}.",
        "day_title": "🌅 Plan for the day",
        "evening_title": "🌇 Evening report",
        "day_summary": "Today: {events} events, {todo} tasks, {lights} lights.",
        "weather": "☀️ Weather",
        "calendar": "📅 Calendar",
        "calendar_today": "📅 Today",
        "calendar_tomorrow": "📅 Tomorrow",
        "home": "🏠 Home",
        "todo": "🛒 Shopping list",
        "mail": "✉️ Mail",
        "tomorrow": "Tomorrow:",
        "all_day": "all day",
        "no_events": "no events",
        "no_todo": "the list is empty",
        "no_mail": "no new mail",
        "all_quiet": "all quiet",
        "light": "light on: {items}",
        "lights": "lights on: {count} — {items}",
        "openings": "open: {items}",
        "more": "… and {count} more",
        "unavailable": "{section}: unavailable",
        "feels_like": "feels like {value}",
        "weekdays": (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ),
        "months": (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        "weather_states": {
            "clear-night": "clear",
            "cloudy": "cloudy",
            "exceptional": "exceptional",
            "fog": "fog",
            "hail": "hail",
            "lightning": "lightning",
            "lightning-rainy": "lightning and rain",
            "partlycloudy": "partly cloudy",
            "pouring": "pouring",
            "rainy": "rainy",
            "snowy": "snowy",
            "snowy-rainy": "snow and rain",
            "sunny": "sunny",
            "windy": "windy",
            "windy-variant": "windy and cloudy",
        },
    },
}


def parse_brief_time(value: object) -> tuple[int, int]:
    """Return ``(hour, minute)`` from an options value."""
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) >= 2:
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError:
                return _default_brief_time()
            if 0 <= hour < 24 and 0 <= minute < 60:
                return hour, minute
    return _default_brief_time()


def _default_brief_time() -> tuple[int, int]:
    """Return the default briefing time as a pair."""
    hour, _, minute = DEFAULT_BRIEF_TIME.partition(":")
    return int(hour), int(minute)


def is_quiet_hours(
    now: datetime | None = None,
    quiet_start: str = DEFAULT_QUIET_START,
    quiet_end: str = DEFAULT_QUIET_END,
) -> bool:
    """Return True when the current local time is inside the quiet interval.

    Quiet hours span midnight (e.g. 23:00-07:00). Both values are HH:MM
    strings. Returns False when either value is empty or malformed.
    """
    if not quiet_start or not quiet_end:
        return False
    current = now or dt_util.now()
    try:
        # The options flow stores HH:MM:SS; seconds do not matter here.
        start_h, start_m = (int(x) for x in quiet_start.split(":")[:2])
        end_h, end_m = (int(x) for x in quiet_end.split(":")[:2])
    except (ValueError, TypeError):
        return False
    current_minutes = current.hour * 60 + current.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    # Spans midnight
    return current_minutes >= start_minutes or current_minutes <= end_minutes


async def async_build_briefing(
    hass: HomeAssistant,
    sources: BriefingSources,
    language: str,
    *,
    mailbox: Mailbox | None = None,
    allow_cloud_for_mail: bool = False,
    kind: str | None = None,
    extra_lines: list[str] | None = None,
    personal_mail: str | None = None,
) -> str:
    """Build the briefing text for one chat.

    ``extra_lines`` come from connection plugins and follow the home status
    in the morning and full briefing. ``personal_mail`` is the digest of the
    user's own mailbox and replaces the shared mail section.

    ``kind`` is ``"morning"`` (plan for the day — calendar, weather, todo,
    home, mail), ``"evening"`` (what happened today — shorter summary) or
    ``None`` (all blocks, matching the old full briefing).
    """
    texts = _TEXTS.get(language, _TEXTS[DEFAULT_LANGUAGE])
    now = dt_util.now()

    blocks = [_header(now, texts)]

    if kind == BRIEFING_EVENING:
        blocks.append(await _async_evening_summary(hass, sources, texts))
    else:
        for block in (
            await _async_weather(hass, sources, texts),
            await _async_calendar(hass, sources, texts, now),
            _home_status(hass, sources, texts),
            "\n".join(extra_lines or ()),
            await _async_todo(hass, sources, texts),
            personal_mail
            if personal_mail is not None
            else await async_mail_digest(
                hass,
                sources,
                language,
                mailbox=mailbox,
                allow_cloud=allow_cloud_for_mail,
            ),
        ):
            if block:
                blocks.append(block)
    return "\n\n".join(blocks)


def texts_for(language: str) -> dict[str, Any]:
    """Return the localized briefing strings."""
    return _TEXTS.get(language, _TEXTS[DEFAULT_LANGUAGE])


async def _async_evening_summary(
    hass: HomeAssistant, sources: BriefingSources, texts: dict[str, Any]
) -> str:
    """Build a short evening summary: what was on today, what is on now."""
    lines = [texts["evening_title"]]

    # Count of today's calendar events
    today_events: int = 0
    if sources.has_calendars:
        start = dt_util.start_of_local_day(dt_util.now())
        tomorrow = start + timedelta(days=1)
        events, _ = await _async_fetch_events(hass, sources, start, tomorrow)
        today_events = sum(1 for event in events if start <= event[0] < tomorrow)

    todo_count: int = 0
    if sources.todo:
        try:
            result = await hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": sources.todo, "status": "needs_action"},
                blocking=True,
                return_response=True,
            )
            if isinstance(result, dict):
                entity_result = result.get(sources.todo)
                if isinstance(entity_result, dict):
                    items = entity_result.get("items")
                    if isinstance(items, list):
                        todo_count = len(items)
        except HomeAssistantError:
            pass

    home_light_count: int = 0
    if sources.home:
        lights = 0
        for entity_id in hass.states.async_entity_ids("light"):
            state = hass.states.get(entity_id)
            if state is not None and state.state == "on":
                lights += 1
        home_light_count = lights

    parts = []
    if today_events:
        parts.append(texts["day_summary"].format(
            events=today_events, todo=todo_count, lights=home_light_count
        ))
    if not parts:
        parts.append(texts["all_quiet"])
    lines.extend(f"• {part}" for part in parts)
    return "\n".join(lines)


async def _async_fetch_events(
    hass: HomeAssistant, sources: BriefingSources, start: datetime, end: datetime
) -> tuple[list[tuple[datetime, str, bool]], bool]:
    """Return merged HA and personal events, and whether HA calendars failed."""
    events: list[tuple[datetime, str, bool]] = []
    failed = False
    if sources.calendars:
        try:
            response = await hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                },
                target={"entity_id": list(sources.calendars)},
                blocking=True,
                return_response=True,
            )
            events.extend(_parse_events(response))
        except HomeAssistantError as err:
            _LOGGER.debug("Calendar is unavailable (%s)", type(err).__name__)
            failed = True
    if sources.ics:
        from .personal_calendar import async_ics_events

        events.extend(await async_ics_events(hass, sources.ics, start, end))
    seen: set[tuple[datetime, str]] = set()
    merged = []
    for event in events:
        if (event[0], event[1]) not in seen:
            seen.add((event[0], event[1]))
            merged.append(event)
    merged.sort(key=lambda item: (not item[2], item[0]))
    return merged, failed


async def async_events_between(
    hass: HomeAssistant,
    sources: BriefingSources,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, str, bool]]:
    """Return ``(start, summary, all_day)`` of events starting in ``[start, end)``.

    An unavailable calendar gives an empty list; callers poll every minute and
    must not fail the whole tick.
    """
    if not sources.has_calendars:
        return []
    events, _ = await _async_fetch_events(hass, sources, start, end)
    return [event for event in events if start <= event[0] < end]


async def async_calendar_day(
    hass: HomeAssistant,
    sources: BriefingSources,
    language: str,
    *,
    tomorrow: bool = False,
) -> str | None:
    """Return one day of events, or None when no calendar is configured."""
    if not sources.has_calendars:
        return None

    texts = texts_for(language)
    start = dt_util.start_of_local_day(dt_util.now()) + timedelta(
        days=1 if tomorrow else 0
    )
    end = start + timedelta(days=1)
    events, failed = await _async_fetch_events(hass, sources, start, end)
    if failed and not events:
        return texts["unavailable"].format(section=texts["calendar"])
    events = [event for event in events if start <= event[0] < end]
    _LOGGER.debug("Skill read %s calendar events", len(events))

    lines = [texts["calendar_tomorrow" if tomorrow else "calendar_today"]]
    if events:
        lines.extend(_format_event(event, texts) for event in events[:MAX_EVENTS])
        if len(events) > MAX_EVENTS:
            lines.append(texts["more"].format(count=len(events) - MAX_EVENTS))
    else:
        lines.append(texts["no_events"])
    return "\n".join(lines)


def home_status_text(
    hass: HomeAssistant, sources: BriefingSources, language: str
) -> str | None:
    """Return the home summary, or None when the section is disabled."""
    return _home_status(hass, sources, texts_for(language))


def _header(now: datetime, texts: dict[str, Any]) -> str:
    """Return the greeting with the local date."""
    if 5 <= now.hour < 12:
        greeting = texts["morning"]
    elif 12 <= now.hour < 18:
        greeting = texts["day"]
    else:
        greeting = texts["evening"]
    date_line = texts["date"].format(
        weekday=texts["weekdays"][now.weekday()],
        day=now.day,
        month=texts["months"][now.month - 1],
    )
    return f"{greeting} {date_line}"


async def _async_weather(
    hass: HomeAssistant, sources: BriefingSources, texts: dict[str, Any]
) -> str | None:
    """Describe the selected weather entity."""
    if not sources.weather:
        return None
    state = hass.states.get(sources.weather)
    if state is None or state.state in ("unknown", "unavailable"):
        return texts["unavailable"].format(section=texts["weather"])

    description = texts["weather_states"].get(state.state, state.state)
    parts = [description]
    unit = hass.config.units.temperature_unit
    temperature = _number(state.attributes.get("temperature"))
    if temperature is not None:
        parts.append(f"{temperature:+d} {unit}")
    apparent = _number(state.attributes.get("apparent_temperature"))
    if apparent is not None and apparent != temperature:
        parts.append(texts["feels_like"].format(value=f"{apparent:+d} {unit}"))

    return f"{texts['weather']}: {', '.join(parts)}"


async def _async_calendar(
    hass: HomeAssistant,
    sources: BriefingSources,
    texts: dict[str, Any],
    now: datetime,
) -> str | None:
    """List today's events, and tomorrow's when today is empty."""
    if not sources.has_calendars:
        return None

    start = dt_util.start_of_local_day(now)
    tomorrow = start + timedelta(days=1)
    events, failed = await _async_fetch_events(
        hass, sources, start, start + timedelta(days=2)
    )
    if failed and not events:
        return texts["unavailable"].format(section=texts["calendar"])
    _LOGGER.debug("Briefing read %s calendar events", len(events))

    today = [event for event in events if event[0] < tomorrow]
    upcoming = [event for event in events if event[0] >= tomorrow]

    lines = [texts["calendar"]]
    if today:
        lines.extend(_format_event(event, texts) for event in today[:MAX_EVENTS])
    else:
        lines.append(texts["no_events"])

    if upcoming and len(today) < MAX_EVENTS:
        lines.append(texts["tomorrow"])
        remaining = MAX_EVENTS - len(today)
        lines.extend(_format_event(event, texts) for event in upcoming[:remaining])
    return "\n".join(lines)


def _parse_events(response: Any) -> list[tuple[datetime, str, bool]]:
    """Return ``(start, summary, all_day)`` triples from a service response."""
    if not isinstance(response, dict):
        return []

    parsed: list[tuple[datetime, str, bool]] = []
    seen: set[tuple[datetime, str]] = set()
    for entity_result in response.values():
        if not isinstance(entity_result, dict):
            continue
        raw_events = entity_result.get("events")
        if not isinstance(raw_events, list):
            continue
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            summary = raw.get("summary")
            start, all_day = _parse_start(raw.get("start"))
            if not isinstance(summary, str) or not summary.strip() or start is None:
                continue
            key = (start, summary.strip())
            # The same event may be shared by several calendars.
            if key in seen:
                continue
            seen.add(key)
            parsed.append((start, summary.strip(), all_day))

    parsed.sort(key=lambda item: (not item[2], item[0]))
    return parsed


def _format_event(event: tuple[datetime, str, bool], texts: dict[str, Any]) -> str:
    """Render one event as a Telegram-friendly line."""
    start, summary, all_day = event
    time_label = texts["all_day"] if all_day else f"{start:%H:%M}"
    return f"• {time_label} — {summary}"


def _parse_start(value: object) -> tuple[datetime | None, bool]:
    """Parse a calendar event start and tell whether it is all-day."""
    if not isinstance(value, str) or not value:
        return None, False

    all_day = len(value) == 10
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        day = dt_util.parse_date(value)
        if day is None:
            return None, all_day
        return dt_util.start_of_local_day(day), True
    if parsed.tzinfo is None:
        return dt_util.start_of_local_day(parsed.date()), all_day
    return dt_util.as_local(parsed), all_day


def _home_status(
    hass: HomeAssistant, sources: BriefingSources, texts: dict[str, Any]
) -> str | None:
    """Summarise lights, open doors and windows, and active climate."""
    if not sources.home:
        return None

    light_names: list[str] = []
    for entity_id in hass.states.async_entity_ids("light"):
        state = hass.states.get(entity_id)
        if state is None or state.state != "on" or _is_technical_light(state):
            continue
        name = _name(state)
        if name not in light_names:
            light_names.append(name)

    openings: list[str] = []
    for state in hass.states.async_all("binary_sensor"):
        if state.state != "on":
            continue
        if state.attributes.get("device_class") not in _OPENING_CLASSES:
            continue
        openings.append(_name(state))

    climates: list[str] = []
    for state in hass.states.async_all("climate"):
        if state.state in ("off", "unavailable", "unknown"):
            continue
        current = _number(state.attributes.get("current_temperature"))
        if current is None:
            climates.append(_name(state))
        else:
            unit = hass.config.units.temperature_unit
            climates.append(f"{_name(state)} {current:+d} {unit}")

    lines: list[str] = []
    if light_names:
        shown = _join_limited(light_names, MAX_LIGHTS, texts)
        key = "light" if len(light_names) == 1 else "lights"
        lines.append(texts[key].format(count=len(light_names), items=shown))
    if openings:
        shown = _join_limited(openings, MAX_OPENINGS, texts)
        lines.append(texts["openings"].format(items=shown))
    if climates:
        shown = _join_limited(climates, MAX_CLIMATES, texts)
        lines.append(shown)

    if not lines:
        lines.append(texts["all_quiet"])
    return "\n".join([texts["home"], *(f"• {line}" for line in lines)])


async def _async_todo(
    hass: HomeAssistant, sources: BriefingSources, texts: dict[str, Any]
) -> str | None:
    """List the first open shopping items."""
    if not sources.todo:
        return None
    try:
        response = await hass.services.async_call(
            "todo",
            "get_items",
            {"entity_id": sources.todo, "status": "needs_action"},
            blocking=True,
            return_response=True,
        )
    except HomeAssistantError as err:
        _LOGGER.debug("Todo list is unavailable (%s)", type(err).__name__)
        return texts["unavailable"].format(section=texts["todo"])

    entity_result = response.get(sources.todo) if isinstance(response, dict) else None
    raw_items = entity_result.get("items") if isinstance(entity_result, dict) else None
    items = [
        item["summary"].strip()
        for item in raw_items or []
        if isinstance(item, dict) and isinstance(item.get("summary"), str)
    ]
    if not items:
        return f"{texts['todo']}: {texts['no_todo']}"

    lines = [texts["todo"], *(f"• {item}" for item in items[:MAX_TODO_ITEMS])]
    if len(items) > MAX_TODO_ITEMS:
        lines.append(texts["more"].format(count=len(items) - MAX_TODO_ITEMS))
    return "\n".join(lines)


def _join_limited(items: list[str], limit: int, texts: dict[str, Any]) -> str:
    """Join the first ``limit`` items and count the rest."""
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown = f"{shown} {texts['more'].format(count=len(items) - limit)}"
    return shown


def _number(value: object) -> int | None:
    """Round a numeric attribute or state to an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value)
    if isinstance(value, str):
        try:
            return round(float(value.replace(",", ".")))
        except ValueError:
            return None
    return None


def _name(state: Any) -> str:
    """Return a human friendly name of an entity state."""
    name = state.attributes.get("friendly_name")
    return name if isinstance(name, str) and name else state.entity_id


def _is_technical_light(state: Any) -> bool:
    """Tell screens and indicator LEDs apart from actual room lighting."""
    if state.entity_id.lower().endswith(_TECHNICAL_LIGHT_SUFFIXES):
        return True
    name = _name(state).lower()
    return any(word in name for word in _TECHNICAL_LIGHT_WORDS)


__all__ = [
    "BriefingSources",
    "async_build_briefing",
    "parse_brief_time",
]
