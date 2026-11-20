"""Persistent per-chat state for Hearthkeeper.

Only routing data and personal preferences live here: conversation id, agent,
reply mode, language, timestamps and the briefing and quiet-hour choices made
in the Mini App. Message texts, audio, calendar and mail content never reach
disk.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_CHATS, STORAGE_VERSION


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
    brief_enabled: bool | None = None
    brief_time: str | None = None
    # ``[start, end]``; an empty list turns quiet hours off for this chat.
    quiet: list[str] | None = None
    # Overridden briefing sources only: weather, calendars, mail, home.
    sources: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted representation."""
        data = asdict(self)
        data.pop("chat_id")
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
            brief_enabled=_bool_or_none(data.get("brief_enabled")),
            brief_time=_str_or_none(data.get("brief_time")),
            quiet=_quiet_or_none(data.get("quiet")),
            sources=_sources_or_none(data.get("sources")),
        )

    @property
    def has_prefs(self) -> bool:
        """Return True when any personal preference is set."""
        return (
            self.brief_enabled is not None
            or self.brief_time is not None
            or self.quiet is not None
            or bool(self.sources)
        )

    def clear_prefs(self) -> None:
        """Go back to the integration options for every preference."""
        self.brief_enabled = None
        self.brief_time = None
        self.quiet = None
        self.sources = None


def _str_or_none(value: object) -> str | None:
    """Return value when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: object) -> bool | None:
    """Return value when it is a bool."""
    return value if isinstance(value, bool) else None


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
