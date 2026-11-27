"""Tests for calendars users add by a secret iCal link."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.hearthkeeper.brief import BriefingSources, async_events_between
from custom_components.hearthkeeper.personal_calendar import (
    CalendarError,
    PersonalCalendars,
    async_fetch_ics,
    normalise_url,
)

URL = "https://calendar.example.com/secret/basic.ics"
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//test
BEGIN:VEVENT
UID:1
DTSTAMP:20261101T000000Z
DTSTART:20261120T080000Z
DTEND:20261120T090000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Планёрка
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTAMP:20261101T000000Z
DTSTART;VALUE=DATE:20261122
SUMMARY:День рождения Маши
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTAMP:20261101T000000Z
DTSTART;VALUE=DATE:20261119
DTEND;VALUE=DATE:20261123
SUMMARY:Отпуск
END:VEVENT
END:VCALENDAR
"""


def test_normalise_url() -> None:
    """Only https links are accepted; webcal is read as https."""
    assert normalise_url(" webcal://calendar.example.com/a.ics ") == (
        "https://calendar.example.com/a.ics"
    )
    # A calendar served from the home network is allowed on purpose.
    assert normalise_url("https://nas.local/a.ics") == "https://nas.local/a.ics"
    for bad in (
        "http://calendar.example.com/a.ics",
        "ftp://x/a.ics",
        "https://user:pass@calendar.example.com/a.ics",
        "",
        None,
    ):
        with pytest.raises(CalendarError):
            normalise_url(bad)


async def test_only_https_redirects_and_real_calendars(
    hass: HomeAssistant, aioclient_mock: Any
) -> None:
    """A redirect to plain http is refused, and only iCal replies are kept."""
    aioclient_mock.get(URL, status=302, headers={"Location": "http://router.lan/x"})
    with pytest.raises(CalendarError) as err:
        await async_fetch_ics(hass, URL)
    assert err.value.code == "bad_url"

    aioclient_mock.clear_requests()
    aioclient_mock.get("https://router.lan/", text="<html>admin</html>")
    with pytest.raises(CalendarError) as err:
        await async_fetch_ics(hass, "https://router.lan/")
    assert err.value.code == "not_ics"


async def test_add_calendar_and_read_events(
    hass: HomeAssistant,
    aioclient_mock: Any,
    hass_storage: dict[str, Any],
) -> None:
    """A link is checked, stored privately and feeds the user's events."""
    aioclient_mock.get(URL, text=ICS)
    aioclient_mock.get("https://calendar.example.com/page.html", text="<html>no</html>")
    calendars = PersonalCalendars(hass)
    await calendars.async_load()

    with pytest.raises(CalendarError) as err:
        await calendars.async_add(42, "Сайт", "https://calendar.example.com/page.html")
    assert err.value.code == "not_ics"

    item, _ = await calendars.async_add(42, " Работа ", URL)
    assert item.public() == {
        "id": item.id,
        "name": "Работа",
        "host": "calendar.example.com",
    }
    with pytest.raises(CalendarError) as err:
        await calendars.async_add(42, "Again", URL)
    assert err.value.code == "duplicate"

    restored = PersonalCalendars(hass)
    await restored.async_load()
    assert restored.urls(42) == (URL,)
    assert restored.urls(7) == ()

    tz = dt_util.get_default_time_zone()
    sources = BriefingSources(ics=restored.urls(42))
    day = datetime(2026, 11, 22, tzinfo=tz)
    events = await async_events_between(hass, sources, day, day + timedelta(days=1))
    summaries = [summary for _, summary, _ in events]
    # The daily meeting repeats into the 22nd and the birthday is all-day.
    # The holiday started on the 19th and still covers the 22nd.
    assert set(summaries[:2]) == {"День рождения Маши", "Отпуск"}
    assert summaries[2] == "Планёрка"
    assert all(all_day and begins == day for begins, _, all_day in events[:2])

    # On the 23rd the holiday is over.
    after = day + timedelta(days=1)
    later = await async_events_between(hass, sources, after, after + timedelta(days=1))
    assert "Отпуск" not in [summary for _, summary, _ in later]

    await restored.async_remove(42, item.id)
    assert restored.urls(42) == ()
