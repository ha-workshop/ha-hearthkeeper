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

import uuid
from collections.abc import Collection, Mapping
from typing import Any

from .const import BRIEFING_KINDS
from .store import (
    EVENT_REMINDER_CHOICES,
    MASCOT_COLORS,
    MASCOT_NAME_MAX,
    SKILLS_MAX,
    BriefingPlan,
    ChatContext,
    CustomSkill,
    normalise_time,
)

PREF_KEYS = frozenset(
    (
        "reset",
        "briefings",
        "quiet",
        "weather",
        "calendars",
        "mail",
        "home",
        "skills",
        "event_reminder",
        "birthdays",
        "mascot",
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
        elif key == "event_reminder":
            if isinstance(value, bool) or value not in EVENT_REMINDER_CHOICES:
                raise InvalidPreference(key)
            updates[key] = value
        elif key == "mascot":
            if not isinstance(value, Mapping):
                raise InvalidPreference(key)
            color = value.get("color", "")
            name = value.get("name", "")
            if color not in ("", *MASCOT_COLORS) or not isinstance(name, str):
                raise InvalidPreference(key)
            if len(name.strip()) > MASCOT_NAME_MAX:
                raise InvalidPreference(key)
            updates["mascot_color"] = color
            updates["mascot_name"] = name.strip()
        elif key == "birthdays":
            updates[key] = "" if value in ("", None) else _time(key, value)
        elif key == "skills":
            updates["custom_skills"] = _skills(value, chat.custom_skills)
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
    if not isinstance(kind, str) or kind not in BRIEFING_KINDS:
        return None
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None
    name = raw.get("name", "")
    if not isinstance(name, str) or len(name) > 40:
        return None
    raw_id = raw.get("id")
    plan_id = raw_id if isinstance(raw_id, str) and 0 < len(raw_id) <= 32 else uuid.uuid4().hex[:8]  # type: ignore[attr-defined]
    return BriefingPlan(id=plan_id, kind=kind, time=time, name=name.strip(), enabled=enabled)


def _skills(value: object, current: list[CustomSkill]) -> list[CustomSkill]:
    """Return validated custom skills; the bot alone sets ``last_run``."""
    if not isinstance(value, list) or len(value) > SKILLS_MAX:
        raise InvalidPreference("skills")
    last_runs = {skill.id: skill.last_run for skill in current}
    skills: list[CustomSkill] = []
    for raw in value:
        if isinstance(raw, dict):
            raw = {**raw, "last_run": last_runs.get(str(raw.get("id")), "")}
        skill = CustomSkill.from_dict(raw)
        if skill is None:
            raise InvalidPreference("skills")
        skills.append(skill)
    if len({skill.id for skill in skills}) != len(skills):
        raise InvalidPreference("skills")
    return skills


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