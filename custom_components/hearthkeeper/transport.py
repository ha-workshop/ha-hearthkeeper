"""Bus transport for Hearthkeeper.

Hearthkeeper does not talk to Telegram itself: the core ``telegram_bot``
integration owns the connection and the allowlist and fires bus events. This
module turns those events into :class:`IncomingMessage` objects, filters them,
serialises work per chat and sends replies back through ``telegram_bot``
services.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DEDUP_CACHE_SIZE,
    EVENT_ATTACHMENT,
    EVENT_CALLBACK,
    EVENT_COMMAND,
    EVENT_TEXT,
    SENT_ID_CACHE_SIZE,
    TELEGRAM_DOMAIN,
    TELEGRAM_MESSAGE_LIMIT,
)

_LOGGER = logging.getLogger(__name__)

# Keys of the telegram_bot bus events and service arguments. Kept local so
# Hearthkeeper does not import another integration's private constants.
_DATA_BOT = "bot"
_DATA_CONFIG_ENTRY_ID = "config_entry_id"
_DATA_CHAT_ID = "chat_id"
_DATA_ID = "id"
_DATA_TEXT = "text"
_DATA_USER_ID = "user_id"
_DATA_THREAD_ID = "message_thread_id"
_DATA_REPLY_TO = "reply_to_message_id"
_DATA_COMMAND = "command"
_DATA_ARGS = "args"
_DATA_FILE_ID = "file_id"
_DATA_FILE_MIME = "file_mime_type"
_DATA_FILE_SIZE = "file_size"
_DATA_FILE_NAME = "file_name"
_DATA_BOT_USERNAME = "username"
_DATA_CALLBACK_DATA = "data"
_DATA_CALLBACK_MESSAGE = "message"
_CALLBACK_MESSAGE_ID = "message_id"

_SERVICE_SEND_MESSAGE = "send_message"
_SERVICE_SEND_VOICE = "send_voice"
_SERVICE_SEND_CHAT_ACTION = "send_chat_action"
_SERVICE_EDIT_MESSAGE = "edit_message"
_SERVICE_ANSWER_CALLBACK = "answer_callback_query"
_SERVICE_DOWNLOAD_FILE = "download_file"

_DATA_FILE_PATH = "file_path"

# Telegram rejects inline keyboard payloads longer than 64 bytes.
CALLBACK_DATA_LIMIT = 64


class MessageKind(StrEnum):
    """Kind of an incoming Telegram message."""

    TEXT = "text"
    COMMAND = "command"
    ATTACHMENT = "attachment"
    CALLBACK = "callback"


@dataclass(frozen=True, slots=True)
class Attachment:
    """File attached to a Telegram message."""

    file_id: str
    mime_type: str | None = None
    file_size: int | None = None
    file_name: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A Telegram message reduced to what Hearthkeeper needs."""

    kind: MessageKind
    chat_id: int
    message_id: int | None = None
    user_id: int | None = None
    text: str = ""
    thread_id: int | None = None
    reply_to_id: int | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    attachment: Attachment | None = None
    callback_id: str | None = None
    callback_data: str | None = None


def parse_event(kind: MessageKind, data: Mapping[str, Any]) -> IncomingMessage | None:
    """Build an incoming message from telegram_bot event data."""
    chat_id = data.get(_DATA_CHAT_ID)
    if not isinstance(chat_id, int):
        return None

    message_id = data.get(_DATA_ID)
    user_id = data.get(_DATA_USER_ID)
    thread_id = data.get(_DATA_THREAD_ID)
    reply_to = data.get(_DATA_REPLY_TO)
    common: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id if isinstance(message_id, int) else None,
        "user_id": user_id if isinstance(user_id, int) else None,
        "thread_id": thread_id if isinstance(thread_id, int) else None,
        "reply_to_id": reply_to if isinstance(reply_to, int) else None,
    }

    if kind is MessageKind.CALLBACK:
        callback_data = data.get(_DATA_CALLBACK_DATA)
        if not isinstance(callback_data, str) or not callback_data:
            return None
        callback_id = data.get(_DATA_ID)
        raw_message = data.get(_DATA_CALLBACK_MESSAGE)
        # For callbacks ``id`` is the callback query id, not the message id.
        common["message_id"] = None
        if isinstance(raw_message, dict):
            raw_message_id = raw_message.get(_CALLBACK_MESSAGE_ID)
            if isinstance(raw_message_id, int):
                common["message_id"] = raw_message_id
        return IncomingMessage(
            kind=kind,
            text=callback_data,
            callback_id=(
                str(callback_id) if isinstance(callback_id, (str, int)) else None
            ),
            callback_data=callback_data,
            **common,
        )

    if kind is MessageKind.TEXT:
        text = data.get(_DATA_TEXT)
        if not isinstance(text, str) or not text.strip():
            return None
        return IncomingMessage(kind=kind, text=text.strip(), **common)

    if kind is MessageKind.COMMAND:
        command = data.get(_DATA_COMMAND)
        if not isinstance(command, str) or not command.startswith("/"):
            return None
        raw_args = data.get(_DATA_ARGS) or ()
        if not isinstance(raw_args, (list, tuple)):
            raw_args = ()
        args = tuple(str(arg) for arg in raw_args)
        return IncomingMessage(
            kind=kind,
            text=" ".join((command, *args)),
            command=command,
            args=args,
            **common,
        )

    file_id = data.get(_DATA_FILE_ID)
    if not isinstance(file_id, str) or not file_id:
        return None
    caption = data.get(_DATA_TEXT)
    mime = data.get(_DATA_FILE_MIME)
    size = data.get(_DATA_FILE_SIZE)
    name = data.get(_DATA_FILE_NAME)
    return IncomingMessage(
        kind=kind,
        text=caption if isinstance(caption, str) else "",
        attachment=Attachment(
            file_id=file_id,
            mime_type=mime if isinstance(mime, str) else None,
            file_size=size if isinstance(size, int) else None,
            file_name=name if isinstance(name, str) else None,
        ),
        **common,
    )


def strip_bot_mention(text: str, username: str | None) -> str:
    """Remove ``@bot`` from a group message."""
    if not username:
        return text.strip()
    pattern = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
    return pattern.sub("", text).strip()


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split a reply into Telegram-sized chunks without cutting words."""
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        cut = max(cut, 1)
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class Transport:
    """Listen to telegram_bot events and reply through its services."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        telegram_entry_id: str,
        allowed_chats: set[int],
        allow_groups: bool,
    ) -> None:
        """Initialise the transport."""
        self.hass = hass
        self._telegram_entry_id = telegram_entry_id
        self._allowed_chats = allowed_chats
        self._allow_groups = allow_groups
        self._handler: Callable[[IncomingMessage], Awaitable[None]] | None = None
        self._bot_username: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[int, asyncio.Lock] = {}
        self._seen: OrderedDict[tuple[int, object], None] = OrderedDict()
        self._sent_ids: dict[int, deque[int]] = {}

    @property
    def bot_username(self) -> str | None:
        """Return the bot username, learned from the first event."""
        return self._bot_username

    @callback
    def set_handler(
        self, handler: Callable[[IncomingMessage], Awaitable[None]]
    ) -> None:
        """Set the coroutine that processes accepted messages."""
        self._handler = handler

    @callback
    def async_start(self) -> None:
        """Subscribe to the telegram_bot events."""
        for event, kind in (
            (EVENT_TEXT, MessageKind.TEXT),
            (EVENT_COMMAND, MessageKind.COMMAND),
            (EVENT_ATTACHMENT, MessageKind.ATTACHMENT),
            (EVENT_CALLBACK, MessageKind.CALLBACK),
        ):
            self._unsubs.append(
                self.hass.bus.async_listen(event, self._make_listener(kind))
            )

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and cancel outstanding work."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._locks.clear()
        self._sent_ids.clear()

    @callback
    def _make_listener(self, kind: MessageKind) -> Callable[[Event], None]:
        """Return a bus listener for one event kind."""

        @callback
        def _listener(event: Event) -> None:
            self._handle_event(kind, event.data)

        return _listener

    @callback
    def _handle_event(self, kind: MessageKind, data: Mapping[str, Any]) -> None:
        """Filter an event and queue it for the chat worker."""
        bot = data.get(_DATA_BOT)
        if (
            not isinstance(bot, dict)
            or bot.get(_DATA_CONFIG_ENTRY_ID) != self._telegram_entry_id
        ):
            return
        username = bot.get(_DATA_BOT_USERNAME)
        if isinstance(username, str):
            self._bot_username = username

        message = parse_event(kind, data)
        if message is None or not self._accept(message) or self._is_duplicate(message):
            return

        if self._handler is None:
            _LOGGER.warning("No message handler registered, dropping message")
            return

        task = self.hass.async_create_task(
            self._async_dispatch(message), f"hearthkeeper chat {message.chat_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _async_dispatch(self, message: IncomingMessage) -> None:
        """Process one message while holding the chat lock."""
        lock = self._locks.setdefault(message.chat_id, asyncio.Lock())
        async with lock:
            try:
                assert self._handler is not None
                await self._handler(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "Unhandled error while processing a message in chat %s",
                    message.chat_id,
                )

    def _accept(self, message: IncomingMessage) -> bool:
        """Apply the chat allowlist and group rules."""
        if self._allowed_chats and message.chat_id not in self._allowed_chats:
            _LOGGER.debug("Ignoring message from a chat outside the allowlist")
            return False

        if message.chat_id >= 0:
            return True

        if not self._allow_groups:
            _LOGGER.debug("Ignoring group message: group chats are disabled")
            return False

        if message.kind is MessageKind.CALLBACK:
            # Buttons are only ever attached to our own messages, so a tap in
            # an allowed group is always meant for us.
            return True

        if message.kind is MessageKind.COMMAND:
            return True

        if self._mentions_bot(message.text):
            return True

        sent = self._sent_ids.get(message.chat_id)
        return (
            message.reply_to_id is not None
            and sent is not None
            and message.reply_to_id in sent
        )

    def _mentions_bot(self, text: str) -> bool:
        """Return True when the text mentions this bot."""
        return bool(self._bot_username) and (
            f"@{self._bot_username.lower()}" in text.lower()
        )

    def _is_duplicate(self, message: IncomingMessage) -> bool:
        """Drop repeated Telegram updates."""
        if message.message_id is None and message.callback_id is None:
            return False

        # A callback has no message id of its own, so the query id is the key.
        identifier: object = (
            message.message_id
            if message.message_id is not None
            else f"cb:{message.callback_id}"
        )
        key = (message.chat_id, identifier)
        if key in self._seen:
            self._seen.move_to_end(key)
            _LOGGER.debug("Ignoring duplicate message in chat %s", message.chat_id)
            return True

        self._seen[key] = None
        while len(self._seen) > DEDUP_CACHE_SIZE:
            self._seen.popitem(last=False)
        return False

    async def async_send_text(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        keyboard: list[list[list[str]]] | None = None,
    ) -> int | None:
        """Send text to a chat, splitting long replies.

        ``keyboard`` is the ``inline_keyboard`` payload of telegram_bot and is
        attached to the last chunk.
        """
        chunks = split_message(text)
        last_message_id: int | None = None
        for index, chunk in enumerate(chunks):
            last_message_id = await self._async_send_chunk(
                chat_id,
                chunk,
                thread_id=thread_id,
                reply_to=reply_to,
                keyboard=(keyboard if keyboard and index == len(chunks) - 1 else None),
            )
            if last_message_id is not None:
                self._remember_sent(chat_id, last_message_id)
        return last_message_id

    async def _async_send_chunk(
        self,
        chat_id: int,
        chunk: str,
        *,
        thread_id: int | None,
        reply_to: int | None,
        keyboard: list[list[list[str]]] | None = None,
    ) -> int | None:
        """Send one message chunk."""
        data: dict[str, Any] = {
            _DATA_CONFIG_ENTRY_ID: self._telegram_entry_id,
            _DATA_CHAT_ID: chat_id,
            "message": chunk,
            # The message text is plain text, never Telegram markup.
            "parse_mode": "plain_text",
        }
        if thread_id is not None:
            data[_DATA_THREAD_ID] = thread_id
        if reply_to is not None:
            data[_DATA_REPLY_TO] = reply_to
        if keyboard:
            data["inline_keyboard"] = keyboard

        try:
            response = await self._async_call_send_message(data)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not send a Telegram message to chat %s (%s)",
                chat_id,
                type(err).__name__,
            )
            if _DATA_REPLY_TO not in data:
                return None
            # The quoted message may be gone; retry once without the quote.
            data.pop(_DATA_REPLY_TO)
            try:
                response = await self._async_call_send_message(data)
            except HomeAssistantError:
                _LOGGER.warning("Retry without reply_to failed for chat %s", chat_id)
                return None
        return self._extract_message_id(response, chat_id)

    async def _async_call_send_message(self, data: dict[str, Any]) -> Any:
        """Call ``telegram_bot.send_message`` and return its response."""
        try:
            return await self.hass.services.async_call(
                TELEGRAM_DOMAIN,
                _SERVICE_SEND_MESSAGE,
                data,
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError:
            # One retry on network or Telegram error
            await asyncio.sleep(1)
            return await self.hass.services.async_call(
                TELEGRAM_DOMAIN,
                _SERVICE_SEND_MESSAGE,
                data,
                blocking=True,
                return_response=True,
            )

    async def async_edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[list[str]]] | None = None,
    ) -> bool:
        """Edit a message we sent earlier."""
        data: dict[str, Any] = {
            _DATA_CONFIG_ENTRY_ID: self._telegram_entry_id,
            _DATA_CHAT_ID: chat_id,
            "message_id": message_id,
            "message": text,
            "parse_mode": "plain_text",
        }
        if keyboard:
            data["inline_keyboard"] = keyboard
        try:
            await self.hass.services.async_call(
                TELEGRAM_DOMAIN, _SERVICE_EDIT_MESSAGE, data, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not edit a Telegram message in chat %s (%s)",
                chat_id,
                type(err).__name__,
            )
            return False
        return True

    async def async_answer_callback(
        self, callback_id: str | None, text: str = ""
    ) -> None:
        """Stop the button spinner in Telegram."""
        if callback_id is None:
            return
        try:
            query_id = int(callback_id)
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring a callback query with a non-numeric id")
            return
        try:
            await self.hass.services.async_call(
                TELEGRAM_DOMAIN,
                _SERVICE_ANSWER_CALLBACK,
                {"callback_query_id": query_id, "message": text},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Could not answer a callback query (%s)", type(err).__name__)

    async def async_download_file(self, file_id: str, *, file_name: str) -> str | None:
        """Download a Telegram file into the integration's own directory.

        Returns the path of the downloaded file, or ``None`` when Telegram
        refused the download.
        """
        data: dict[str, Any] = {
            _DATA_CONFIG_ENTRY_ID: self._telegram_entry_id,
            _DATA_FILE_ID: file_id,
            _DATA_FILE_NAME: file_name,
        }
        try:
            response = await self.hass.services.async_call(
                TELEGRAM_DOMAIN,
                _SERVICE_DOWNLOAD_FILE,
                data,
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not download a Telegram file (%s)", type(err).__name__
            )
            return None

        if isinstance(response, dict):
            path = response.get(_DATA_FILE_PATH)
            if isinstance(path, str) and path:
                return path
        return None

    async def async_send_voice(
        self,
        chat_id: int,
        file_path: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
    ) -> bool:
        """Upload a staged voice note to a chat."""
        data: dict[str, Any] = {
            _DATA_CONFIG_ENTRY_ID: self._telegram_entry_id,
            _DATA_CHAT_ID: chat_id,
            "file": file_path,
        }
        if thread_id is not None:
            data[_DATA_THREAD_ID] = thread_id
        if reply_to is not None:
            data[_DATA_REPLY_TO] = reply_to

        try:
            await self.hass.services.async_call(
                TELEGRAM_DOMAIN, _SERVICE_SEND_VOICE, data, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not send a Telegram voice note to chat %s (%s)",
                chat_id,
                type(err).__name__,
            )
            return False
        return True

    async def async_send_chat_action(
        self, chat_id: int, action: str = "typing", *, thread_id: int | None = None
    ) -> None:
        """Show a progress indicator in the chat."""
        data: dict[str, Any] = {
            _DATA_CONFIG_ENTRY_ID: self._telegram_entry_id,
            _DATA_CHAT_ID: chat_id,
            "chat_action": action,
        }
        if thread_id is not None:
            data[_DATA_THREAD_ID] = thread_id
        try:
            await self.hass.services.async_call(
                TELEGRAM_DOMAIN, _SERVICE_SEND_CHAT_ACTION, data, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.debug(
                "Could not send a chat action to chat %s (%s)",
                chat_id,
                type(err).__name__,
            )

    @staticmethod
    def _extract_message_id(response: Any, chat_id: int) -> int | None:
        """Pull the message id out of a telegram_bot service response."""
        if not isinstance(response, dict):
            return None
        chats = response.get("chats")
        if not isinstance(chats, list):
            return None
        for item in chats:
            if isinstance(item, dict) and item.get("chat_id") == chat_id:
                message_id = item.get("message_id")
                if isinstance(message_id, int):
                    return message_id
        return None

    def _remember_sent(self, chat_id: int, message_id: int) -> None:
        """Remember our own messages to recognise replies in groups."""
        bucket = self._sent_ids.setdefault(chat_id, deque(maxlen=SENT_ID_CACHE_SIZE))
        bucket.append(message_id)
