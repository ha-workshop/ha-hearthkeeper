"""Persistent per-chat state for Hearthkeeper.

Only routing data lives here: conversation id, agent, reply mode, language and
timestamps. Message texts, audio, calendar and mail content never reach disk.
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
        )


def _str_or_none(value: object) -> str | None:
    """Return value when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


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
