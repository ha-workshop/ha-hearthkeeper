"""Personal settings a user changes from the Mini App.

Each Telegram user gets their own briefing schedule, quiet hours and briefing
sources, stored with their private chat. Anything not set falls back to the
integration options, which stay the household defaults and are edited only in
Home Assistant.

Calendars and mailboxes are private data, so a user may only pick from those
the integration options already include; the options are the pool an admin
shares with the household. Weather is not private and any weather entity may
be picked.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

from .store import ChatContext

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")

PREF_KEYS = frozenset(
    (
        "reset",
        "brief_enabled",
        "brief_time",
        "quiet",
        "weather",
        "calendars",
        "mail",
        "home",
    )
)


class InvalidPreference(ValueError):
    """A preference the page sent cannot be stored."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def normalise_time(value: object) -> str | None:
    """Return ``HH:MM`` for a valid ``HH:MM`` or ``HH:MM:SS`` string."""
    if not isinstance(value, str):
        return None
    match = _TIME.match(value.strip())
    return f"{match.group(1)}:{match.group(2)}" if match else None


def apply_preferences(
    chat: ChatContext,
    patch: object,
    *,
    weather_ids: Collection[str],
    calendar_pool: Collection[str],
    mail_pool: Collection[str],
) -> list[str]:
    """Validate ``patch`` and store it on ``chat``; return the changed keys.

    Nothing changes unless every key is valid.
    """
    if not isinstance(patch, Mapping) or not patch:
        raise InvalidPreference("prefs")
    unknown = [str(key) for key in patch if key not in PREF_KEYS]
    if unknown:
        raise InvalidPreference(unknown[0])

    updates: dict[str, Any] = {}
    source_updates: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "reset":
            if value is not True:
                raise InvalidPreference(key)
        elif key == "brief_enabled":
            if value is not None and not isinstance(value, bool):
                raise InvalidPreference(key)
            updates[key] = value
        elif key == "brief_time":
            updates[key] = None if value is None else _time(key, value)
        elif key == "quiet":
            updates[key] = _quiet(value)
        elif key == "weather":
            if value is None or value == "":
                source_updates[key] = None
            elif isinstance(value, str) and value in weather_ids:
                source_updates[key] = value
            else:
                raise InvalidPreference(key)
        elif key == "calendars":
            source_updates[key] = _subset(key, value, calendar_pool)
        elif key == "mail":
            source_updates[key] = _subset(key, value, mail_pool)
        elif key == "home":
            if not isinstance(value, bool):
                raise InvalidPreference(key)
            source_updates[key] = value

    if patch.get("reset") is True:
        chat.clear_prefs()
    for key, value in updates.items():
        setattr(chat, key, value)
    if source_updates:
        chat.sources = (chat.sources or {}) | source_updates
    return sorted(str(key) for key in patch)


def _time(key: str, value: object) -> str:
    normalised = normalise_time(value)
    if normalised is None:
        raise InvalidPreference(key)
    return normalised


def _quiet(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) not in (0, 2):
        raise InvalidPreference("quiet")
    return [_time("quiet", item) for item in value]


def _subset(key: str, value: object, pool: Collection[str]) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in pool for item in value
    ):
        raise InvalidPreference(key)
    return list(dict.fromkeys(value))
