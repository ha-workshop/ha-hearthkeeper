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

from collections.abc import Collection, Mapping
from typing import Any

from .store import ChatContext, BriefingPlan, normalise_time

PREF_KEYS = frozenset(
    (
        "reset",
        "briefings",
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
        elif key == "briefings":
            if value is None:
                updates[key] = None
            elif not isinstance(value, list):
                raise InvalidPreference(key)
            else:
                plans: list[BriefingPlan] = []
                for raw in value:
                    plan = _as_briefing_plan(raw)
                    if plan is None:
                        raise InvalidPreference(key)
                    plans.append(plan)
                updates[key] = plans if plans else []
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


def _as_briefing_plan(raw: object) -> BriefingPlan | None:
    """Return a validated plan from a page patch."""
    if not isinstance(raw, dict):
        return None
    time = normalise_time(raw.get("time"))
    if time is None:
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in ("morning", "evening"):
        return None
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None
    name = raw.get("name", "")
    if not isinstance(name, str) or len(name) > 40:
        return None
    return BriefingPlan(kind=kind, time=time, name=name.strip(), enabled=enabled)


def _quiet(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) not in (0, 2):
        raise InvalidPreference("quiet")
    return [_time("quiet", item) for item in value]


def _time(key: str, value: object) -> str:
    normalised = normalise_time(value)
    if normalised is None:
        raise InvalidPreference(key)
    return normalised


def _subset(key: str, value: object, pool: Collection[str]) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in pool for item in value
    ):
        raise InvalidPreference(key)
    return list(dict.fromkeys(value))