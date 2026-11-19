"""Conversation brain: commands, routing and reply modes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .assist import PipelineSettings
from .const import (
    CONVERSATION_TIMEOUT,
    DEFAULT_LANGUAGE,
    REPLY_MODE_BOTH,
    REPLY_MODE_TEXT,
    REPLY_MODE_VOICE,
)
from .shop import ShopSkill
from .store import ChatContext, ChatStore
from .transport import IncomingMessage, MessageKind, Transport, strip_bot_mention

_LOGGER = logging.getLogger(__name__)

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "help": (
            "Я Hearthkeeper — ассистент дома в Telegram.\n\n"
            "Просто напишите вопрос или команду дому.\n\n"
            "Команды:\n"
            "/new — начать новый разговор\n"
            "/voice on|off|both — голосовые ответы\n"
            "/shop — список покупок\n"
            "/brief — сводка дня\n"
            "/model — сменить агента\n"
            "/help — эта справка"
        ),
        "reset_done": "Контекст разговора сброшен.",
        "voice_set": (
            "Режим ответа: {mode}. Голосовые ответы появятся в следующем обновлении."
        ),
        "voice_unknown": (
            "Не понял режим. Используйте /voice on, /voice off или /voice both."
        ),
        "brief_soon": "Брифинг появится в следующем обновлении.",
        "model_soon": "Смена агента появится в следующем обновлении.",
        "attachment_soon": (
            "Голосовые и файлы пока не поддерживаются — напишите текстом."
        ),
        "echo": "Эхо: {text}",
        "timeout": "Ассистент не ответил за минуту. Попробуйте ещё раз.",
        "failed": "Не получилось получить ответ. Попробуйте ещё раз.",
        "empty": "Ассистент вернул пустой ответ.",
    },
    "en": {
        "help": (
            "I am Hearthkeeper, your home assistant in Telegram.\n\n"
            "Just write a question or a command for the house.\n\n"
            "Commands:\n"
            "/new — start a new conversation\n"
            "/voice on|off|both — voice replies\n"
            "/shop — shopping list\n"
            "/brief — daily briefing\n"
            "/model — switch the agent\n"
            "/help — this help"
        ),
        "reset_done": "Conversation context cleared.",
        "voice_set": "Reply mode: {mode}. Voice replies arrive in the next update.",
        "voice_unknown": "Unknown mode. Use /voice on, /voice off or /voice both.",
        "brief_soon": "The briefing is coming in the next update.",
        "model_soon": "Switching the agent is coming in the next update.",
        "attachment_soon": "Voice and files are not supported yet — please send text.",
        "echo": "Echo: {text}",
        "timeout": "The assistant did not answer within a minute. Please try again.",
        "failed": "Could not get an answer. Please try again.",
        "empty": "The assistant returned an empty answer.",
    },
}

_VOICE_MODES = {
    "on": REPLY_MODE_VOICE,
    "voice": REPLY_MODE_VOICE,
    "off": REPLY_MODE_TEXT,
    "text": REPLY_MODE_TEXT,
    "both": REPLY_MODE_BOTH,
}


def extract_speech(response: Any) -> str:
    """Return the plain speech from a ``conversation.process`` response."""
    if not isinstance(response, dict):
        return ""
    result = response.get("response")
    if not isinstance(result, dict):
        return ""
    speech = result.get("speech")
    if not isinstance(speech, dict):
        return ""
    plain = speech.get("plain")
    if not isinstance(plain, dict):
        return ""
    text = plain.get("speech")
    return text.strip() if isinstance(text, str) else ""


class Brain:
    """Turn accepted messages into replies."""

    def __init__(
        self,
        hass: HomeAssistant,
        transport: Transport,
        chats: ChatStore,
        *,
        pipeline: PipelineSettings | None,
        reply_mode: str,
        todo_entity: str | None = None,
    ) -> None:
        """Initialise the brain with entry-level defaults."""
        self.hass = hass
        self._transport = transport
        self._chats = chats
        self._pipeline = pipeline
        self._default_reply_mode = reply_mode
        self._shop = ShopSkill(hass, transport, todo_entity)

    async def async_handle(self, message: IncomingMessage) -> None:
        """Process one accepted message."""
        chat = self._chats.mark_seen(message.chat_id)

        if message.kind is MessageKind.CALLBACK:
            await self._shop.async_handle_callback(message, self._language(chat))
            return

        if message.kind is MessageKind.COMMAND:
            await self._async_handle_command(message, chat)
            return

        if message.kind is MessageKind.ATTACHMENT and not message.text.strip():
            await self._reply(message, self._text(chat, "attachment_soon"))
            return
        # A caption is plain text, so it can go to the agent as is.

        await self._async_answer(message, chat)

    async def _async_answer(self, message: IncomingMessage, chat: ChatContext) -> None:
        """Send the user text to the conversation agent."""
        text = strip_bot_mention(message.text, self._transport.bot_username)
        if not text:
            return

        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )

        agent_id = chat.agent_id or self._agent_id()
        if not agent_id:
            await self._reply(message, self._text(chat, "echo").format(text=text))
            return

        try:
            async with asyncio.timeout(CONVERSATION_TIMEOUT):
                result = await self.hass.services.async_call(
                    "conversation",
                    "process",
                    {
                        "text": text,
                        "agent_id": agent_id,
                        "conversation_id": chat.conversation_id,
                        "language": self._conversation_language(chat),
                    },
                    blocking=True,
                    return_response=True,
                )
        except TimeoutError:
            await self._reply(message, self._text(chat, "timeout"))
            return
        except HomeAssistantError as err:
            # The error text may quote the user message, so log the type only.
            _LOGGER.error(
                "Conversation failed in chat %s (%s)",
                message.chat_id,
                type(err).__name__,
            )
            await self._reply(message, self._text(chat, "failed"))
            return

        reply = extract_speech(result)
        if not reply:
            await self._reply(message, self._text(chat, "empty"))
            return

        conversation_id = None
        if isinstance(result, dict):
            raw_conversation_id = result.get("conversation_id")
            if isinstance(raw_conversation_id, str) and raw_conversation_id:
                conversation_id = raw_conversation_id

        if conversation_id and conversation_id != chat.conversation_id:
            chat.conversation_id = conversation_id
            chat.last_agent = agent_id
            await self._chats.async_save()

        await self._reply(message, reply)

    async def _async_handle_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Handle a /command."""
        command = (message.command or "").split("@", 1)[0].lstrip("/").lower()

        if command in ("start", "help"):
            await self._reply(message, self._text(chat, "help"))
        elif command == "new":
            chat.conversation_id = f"tg:{message.chat_id}"
            await self._chats.async_save()
            await self._reply(message, self._text(chat, "reset_done"))
        elif command == "voice":
            await self._async_voice_command(message, chat)
        elif command == "shop":
            await self._shop.async_handle_command(message, self._language(chat))
        elif command == "brief":
            await self._reply(message, self._text(chat, "brief_soon"))
        elif command == "model":
            await self._reply(message, self._text(chat, "model_soon"))
        else:
            # Another automation may own this command (for example /shop or
            # /video). Staying quiet keeps both working in the same chat.
            _LOGGER.debug("Ignoring command %s: not ours", command)

    async def _async_voice_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Switch the reply mode for this chat."""
        argument = message.args[0].lower() if message.args else ""
        new_mode = _VOICE_MODES.get(argument)
        if new_mode is None:
            await self._reply(message, self._text(chat, "voice_unknown"))
            return

        chat.reply_mode = new_mode
        await self._chats.async_save()
        await self._reply(message, self._text(chat, "voice_set").format(mode=new_mode))

    async def _reply(self, message: IncomingMessage, text: str) -> None:
        """Send a reply in the modes implemented so far."""
        # Voice replies arrive in M2; until then every mode answers with text.
        reply_to = message.message_id if message.chat_id < 0 else None
        await self._transport.async_send_text(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=reply_to,
        )

    def _language(self, chat: ChatContext) -> str:
        """Return the language used for control replies."""
        language = chat.language or self._pipeline_language()
        return language.split("-", 1)[0].lower()

    def _conversation_language(self, chat: ChatContext) -> str:
        """Return the language passed to the conversation agent."""
        if chat.language:
            return chat.language
        if self._pipeline is not None:
            return self._pipeline.conversation_language
        return self._pipeline_language()

    def _pipeline_language(self) -> str:
        """Return the pipeline or instance language."""
        if self._pipeline is not None and self._pipeline.language:
            return self._pipeline.language
        return self.hass.config.language or DEFAULT_LANGUAGE

    def _agent_id(self) -> str | None:
        """Return the conversation agent from the selected pipeline."""
        return self._pipeline.conversation_engine if self._pipeline else None

    def _text(self, chat: ChatContext, key: str) -> str:
        """Return a localised chat string."""
        language = self._language(chat)
        table = TEXTS.get(language, TEXTS[DEFAULT_LANGUAGE])
        return table[key]


__all__ = ["Brain", "extract_speech"]
