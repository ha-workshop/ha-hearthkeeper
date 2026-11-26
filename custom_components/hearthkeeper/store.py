"""Persistent per-chat state for Hearthkeeper.

Only routing data and personal preferences live here: conversation id, agent,
reply mode, language, timestamps and the briefing and quiet-hour choices made
in the Mini App. Message texts, audio, calendar and mail content never reach
disk.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict, dataclass
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BRIEFING_KINDS,
    BRIEFING_MORNING,
    DEFAULT_BRIEF_TIME,
    STORAGE_CHATS,
    STORAGE_VERSION,
)

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")


def normalise_time(value: object) -> str | None:
    """Return ``HH:MM`` for a valid ``HH:MM`` or ``HH:MM:SS`` string."""
    if not isinstance(value, str):
        return None
    match = _TIME.match(value.strip())
    return f"{match.group(1)}:{match.group(2)}" if match else None


@dataclass(slots=True)
class BriefingPlan:
    """One scheduled briefing of a chat.

    ``kind`` picks the content: the morning briefing with the plan for the day
    or the evening report with what actually happened. ``name`` is a personal
    label; empty means the localized default.
    """

    id: str
    kind: str
    time: str
    name: str = ""
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> BriefingPlan | None:
        """Restore a plan, or None when the stored value is malformed."""
        if not isinstance(data, Mapping):
            return None
        kind = data.get("kind")
        if kind not in BRIEFING_KINDS:
            return None
        time = normalise_time(data.get("time"))
        if time is None:
            return None
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            return None
        name = data.get("name", "")
        if not isinstance(name, str) or len(name) > 40:
            return None
        raw_id = data.get("id")
        plan_id = (
            raw_id
            if isinstance(raw_id, str) and 0 < len(raw_id) <= 32
            else uuid.uuid4().hex[:8]
        )
        return cls(
            id=plan_id, kind=kind, time=time, name=name.strip(), enabled=enabled
        )


@dataclass(slots=True)
class ChatContext:
    """Per-chat overrides and conversation state."""

    chat_id: int
    conversation_id: str
    agent_id: str | None = None
    reply_mode: str | None = None
    language: str | None = None
    last_seen: str | None = None
    last_agent: str | None = None
    # Personal preferences. ``None`` means "use the integration options".
    # A list of :class:`BriefingPlan`; ``None`` falls back to the household
    # morning briefing, an empty list means "none at all".
    briefings: list[BriefingPlan] | None = None
    # ``[start, end]``; an empty list turns quiet hours off for this chat.
    quiet: list[str] | None = None
    # Overridden briefing sources only: weather, calendars, mail, home.
    sources: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted representation."""
        data = asdict(self)
        data.pop("chat_id")
        if data.get("briefings") is not None:
            data["briefings"] = [
                plan if isinstance(plan, dict) else plan.as_dict()
                for plan in data["briefings"]
            ]
        return data

    @classmethod
    def from_dict(cls, chat_id: int, data: dict[str, Any]) -> ChatContext:
        """Restore a chat from storage."""
        conversation_id = data.get("conversation_id")
        return cls(
            chat_id=chat_id,
            conversation_id=(
                conversation_id
                if isinstance(conversation_id, str) and conversation_id
                else f"tg:{chat_id}"
            ),
            agent_id=_str_or_none(data.get("agent_id")),
            reply_mode=_str_or_none(data.get("reply_mode")),
            language=_str_or_none(data.get("language")),
            last_seen=_str_or_none(data.get("last_seen")),
            last_agent=_str_or_none(data.get("last_agent")),
            briefings=_briefings_or_none(data.get("briefings")),
            quiet=_quiet_or_none(data.get("quiet")),
            sources=_sources_or_none(data.get("sources")),
        )

    @property
    def has_prefs(self) -> bool:
        """Return True when any personal preference is set."""
        return (
            self.briefings is not None
            or self.quiet is not None
            or bool(self.sources)
        )

    def clear_prefs(self) -> None:
        """Go back to the integration options for every preference."""
        self.briefings = None
        self.quiet = None
        self.sources = None


def _str_or_none(value: object) -> str | None:
    """Return value when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _briefings_or_none(value: object) -> list[BriefingPlan] | None:
    """Return stored briefings, dropping any that fail validation."""
    if not isinstance(value, list):
        return None
    plans = []
    for item in value:
        plan = BriefingPlan.from_dict(item)
        if plan is not None:
            plans.append(plan)
    return plans if plans else []


def _quiet_or_none(value: object) -> list[str] | None:
    """Return a stored quiet interval: two times, or empty for "off"."""
    if not isinstance(value, list):
        return None
    if not value:
        return []
    if len(value) == 2 and all(isinstance(item, str) and item for item in value):
        return list(value)
    return None


def _sources_or_none(value: object) -> dict[str, Any] | None:
    """Return the stored source overrides, dropping anything malformed."""
    if not isinstance(value, dict):
        return None
    sources: dict[str, Any] = {}
    weather = value.get("weather", ...)
    if weather is None or (isinstance(weather, str) and weather):
        sources["weather"] = weather
    for key in ("calendars", "mail"):
        items = value.get(key)
        if isinstance(items, list):
            sources[key] = [item for item in items if isinstance(item, str) and item]
    if isinstance(value.get("home"), bool):
        sources["home"] = value["home"]
    return sources or None


class ChatStore:
    """Per-chat settings with lazy persistence."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_CHATS)
        self._chats: dict[int, ChatContext] = {}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load chats from disk."""
        data = await self._store.async_load() or {}
        chats = data.get("chats")
        if not isinstance(chats, dict):
            return
        for raw_chat_id, raw_chat in chats.items():
            try:
                chat_id = int(raw_chat_id)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_chat, dict):
                self._chats[chat_id] = ChatContext.from_dict(chat_id, raw_chat)

    def get(self, chat_id: int) -> ChatContext:
        """Return the chat, creating it on first use."""
        chat = self._chats.get(chat_id)
        if chat is None:
            chat = ChatContext(chat_id=chat_id, conversation_id=f"tg:{chat_id}")
            self._chats[chat_id] = chat
        return chat

    @property
    def chats(self) -> dict[int, ChatContext]:
        """Return all known chats."""
        return self._chats

    def mark_seen(self, chat_id: int) -> ChatContext:
        """Remember that the chat was active just now."""
        chat = self.get(chat_id)
        chat.last_seen = dt_util.utcnow().isoformat()
        return chat

    async def async_save(self) -> None:
        """Write all chats to disk."""
        chats = {str(chat.chat_id): chat.as_dict() for chat in self._chats.values()}
        async with self._lock:
            await self._store.async_save({"chats": chats})

    async def async_reset(self, chat_id: int | None = None) -> None:
        """Forget one chat or every chat."""
        if chat_id is None:
            self._chats.clear()
        else:
            self._chats.pop(chat_id, None)
        await self.async_save()
