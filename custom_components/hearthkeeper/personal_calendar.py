"""Calendars each Telegram user adds for themselves by a secret iCal link.

Google, Yandex, iCloud and Outlook can publish a calendar as a private
``.ics`` address. A user pastes it in the Mini App; Hearthkeeper reads it for
that user's briefing, calendar skill and reminders only. The calendar never
becomes a Home Assistant entity, so nobody else sees it.

The link works like a password, so it is kept in a private store, never sent
back to the page in full and never logged. Only ``https`` (and ``webcal``,
read as ``https``) is fetched, and a link is kept only when the reply really
is an iCalendar. Local addresses are allowed on purpose, so a calendar served
from the home network works; plain ``http`` devices stay out of reach.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY: Final = f"{DOMAIN}.personal_calendars"
STORAGE_VERSION: Final = 1
MAX_CALENDARS: Final = 5
NAME_MAX: Final = 40
URL_MAX: Final = 2048
_MAX_BYTES: Final = 5 * 1024 * 1024
_TIMEOUT: Final = 20
_TTL: Final = 10 * 60
_REDIRECTS: Final = 3
_CACHE: Final = f"{DOMAIN}_ics_cache"

type Event = tuple[datetime, str, bool]


class CalendarError(Exception):
    """A link cannot be used; ``code`` is safe to show to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class PersonalCalendar:
    """One calendar a user added by link."""

    id: str
    name: str
    url: str

    def public(self) -> dict[str, str]:
        """Return what the page may see: the link only as its host."""
        return {
            "id": self.id,
            "name": self.name,
            "host": urlsplit(self.url).hostname or "",
        }


def normalise_url(value: object) -> str:
    """Return an ``https`` link, or raise ``CalendarError("bad_url")``."""
    if not isinstance(value, str) or not 0 < len(value.strip()) <= URL_MAX:
        raise CalendarError("bad_url")
    url = value.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise CalendarError("bad_url")
    return url


async def async_fetch_ics(hass: HomeAssistant, url: str) -> str:
    """Download a calendar, following a few ``https`` redirects."""
    session = async_get_clientsession(hass)
    for _ in range(_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise CalendarError("bad_url")
        try:
            async with asyncio.timeout(_TIMEOUT):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise CalendarError("unreachable")
                        url = urljoin(url, location)
                        continue
                    if response.status in (401, 403, 404):
                        raise CalendarError("not_found")
                    if response.status != 200:
                        raise CalendarError("unreachable")
                    body = await response.content.read(_MAX_BYTES + 1)
        except (ClientError, TimeoutError) as err:
            raise CalendarError("unreachable") from err
        if len(body) > _MAX_BYTES:
            raise CalendarError("too_large")
        text = body.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text[:2048].upper():
            raise CalendarError("not_ics")
        return text
    raise CalendarError("unreachable")


def _parse(text: str) -> Any:
    """Parse iCalendar text. Runs in the executor."""
    from ical.calendar_stream import IcsCalendarStream
    from ical.exceptions import CalendarParseError

    try:
        return IcsCalendarStream.calendar_from_ics(text)
    except (CalendarParseError, ValueError) as err:
        raise CalendarError("not_ics") from err


def _between(calendar: Any, start: datetime, end: datetime) -> list[Event]:
    """Return events of a parsed calendar that start in ``[start, end)``."""
    events: list[Event] = []
    timeline = calendar.timeline_tz(dt_util.get_default_time_zone())
    for item in timeline.overlapping(start, end):
        summary = (item.summary or "").strip()
        if not summary:
            continue
        begins = item.start
        if isinstance(begins, datetime):
            begins = dt_util.as_local(
                begins
                if begins.tzinfo
                else begins.replace(tzinfo=dt_util.get_default_time_zone())
            )
            all_day = False
        elif isinstance(begins, date):
            # An all-day event that started earlier (a week of holiday) counts
            # on each of its days, so report it on the first day in range.
            begins = max(
                dt_util.start_of_local_day(begins), dt_util.start_of_local_day(start)
            )
            ends = item.end if isinstance(item.end, date) else None
            if ends is not None and dt_util.start_of_local_day(ends) <= begins:
                continue
            all_day = True
        else:
            continue
        if start <= begins < end or (all_day and begins < end):
            events.append((begins, summary, all_day))
    return events


async def _async_calendar(hass: HomeAssistant, url: str) -> Any:
    """Return a parsed calendar from the short cache, or fetch it."""
    cache: dict[str, tuple[float, Any]] = hass.data.setdefault(_CACHE, {})
    cached = cache.get(url)
    if cached is not None and time.monotonic() - cached[0] < _TTL:
        return cached[1]
    text = await async_fetch_ics(hass, url)
    calendar = await hass.async_add_executor_job(_parse, text)
    cache[url] = (time.monotonic(), calendar)
    return calendar


async def async_ics_events(
    hass: HomeAssistant, urls: tuple[str, ...], start: datetime, end: datetime
) -> list[Event]:
    """Return events of every link; a link that fails is skipped."""
    events: list[Event] = []
    for url in urls:
        try:
            calendar = await _async_calendar(hass, url)
            events.extend(
                await hass.async_add_executor_job(_between, calendar, start, end)
            )
        except CalendarError as err:
            _LOGGER.debug("A personal calendar is unavailable (%s)", err.code)
    return events


class PersonalCalendars:
    """Keep each chat's calendar links in a private store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True
        )
        self._calendars: dict[int, list[PersonalCalendar]] = {}

    async def async_load(self) -> None:
        """Read the saved links, dropping anything malformed."""
        data = await self._store.async_load() or {}
        for key, items in data.items():
            try:
                chat_id = int(key)
                self._calendars[chat_id] = [PersonalCalendar(**item) for item in items]
            except (TypeError, ValueError):
                continue

    def get(self, chat_id: int) -> list[PersonalCalendar]:
        """Return the calendars of a chat."""
        return list(self._calendars.get(chat_id, ()))

    def urls(self, chat_id: int) -> tuple[str, ...]:
        """Return the links of a chat's calendars."""
        return tuple(item.url for item in self._calendars.get(chat_id, ()))

    async def async_add(
        self, chat_id: int, name: object, url: object
    ) -> tuple[PersonalCalendar, int]:
        """Check the link is a calendar and save it; return it and today's count."""
        calendars = self._calendars.setdefault(chat_id, [])
        if len(calendars) >= MAX_CALENDARS:
            raise CalendarError("too_many")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= NAME_MAX:
            raise CalendarError("bad_name")
        link = normalise_url(url)
        if any(item.url == link for item in calendars):
            raise CalendarError("duplicate")
        calendar = await _async_calendar(self.hass, link)
        day = dt_util.start_of_local_day()
        today = await self.hass.async_add_executor_job(
            _between, calendar, day, day.replace(hour=23, minute=59, second=59)
        )
        item = PersonalCalendar(id=uuid.uuid4().hex[:8], name=name.strip(), url=link)
        calendars.append(item)
        await self._async_save()
        return item, len(today)

    async def async_remove(self, chat_id: int, calendar_id: object) -> None:
        """Forget one calendar and its link."""
        before = self._calendars.get(chat_id, [])
        after = [item for item in before if item.id != calendar_id]
        if len(after) != len(before):
            self._calendars[chat_id] = after
            await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                str(chat_id): [asdict(item) for item in items]
                for chat_id, items in self._calendars.items()
                if items
            }
        )
