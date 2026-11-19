"""Conversation brain: commands, routing and reply modes."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .assist import PipelineSettings, extract_speech
from .brief import BriefingSources, async_build_briefing
from .const import (
    CONVERSATION_TIMEOUT,
    DEFAULT_LANGUAGE,
    DEFAULT_MAX_VOICE_SECONDS,
    REPLY_MODE_BOTH,
    REPLY_MODE_TEXT,
    REPLY_MODE_VOICE,
    VOICE_DOWNLOAD_LIMIT,
)
from .shop import ShopSkill
from .store import ChatContext, ChatStore
from .transport import IncomingMessage, MessageKind, Transport, strip_bot_mention
from .voice import (
    VoiceTooLongError,
    async_discard_file,
    async_run_audio_pipeline,
    async_stage_voice_reply,
    async_transcode_to_wav,
    audio_suffix,
    is_audio,
)

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
        "voice_set": "Режим ответа: {mode}.",
        "voice_unknown": (
            "Не понял режим. Используйте /voice on, /voice off или /voice both."
        ),
        "voice_no_tts": (
            "Голосовые ответы недоступны: в выбранном пайплайне нет синтеза речи."
        ),
        "voice_no_stt": (
            "Голосовые сообщения недоступны: выберите пайплайн Assist "
            "с распознаванием речи в настройках Hearthkeeper."
        ),
        "voice_too_long": (
            "Голосовое сообщение слишком длинное. Максимум — {seconds} с."
        ),
        "voice_too_big": "Файл слишком большой для расшифровки.",
        "model_soon": "Смена агента появится в следующем обновлении.",
        "attachment_soon": (
            "Пока понимаю текст и голосовые сообщения — отправьте текст или голосовое."
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
        "voice_set": "Reply mode: {mode}.",
        "voice_unknown": "Unknown mode. Use /voice on, /voice off or /voice both.",
        "voice_no_tts": (
            "Voice replies are unavailable: the selected pipeline has no "
            "text-to-speech engine."
        ),
        "voice_no_stt": (
            "Voice messages are unavailable: pick an Assist pipeline with "
            "speech-to-text in the Hearthkeeper options."
        ),
        "voice_too_long": (
            "The voice message is too long. The limit is {seconds} seconds."
        ),
        "voice_too_big": "The file is too large to transcribe.",
        "model_soon": "Switching the agent is coming in the next update.",
        "attachment_soon": (
            "I understand text and voice messages so far — send text or a voice note."
        ),
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
        max_voice_seconds: int = DEFAULT_MAX_VOICE_SECONDS,
        briefing: BriefingSources | None = None,
    ) -> None:
        """Initialise the brain with entry-level defaults."""
        self.hass = hass
        self._transport = transport
        self._chats = chats
        self._pipeline = pipeline
        self._default_reply_mode = reply_mode
        self._max_voice_seconds = max_voice_seconds
        self._briefing = briefing or BriefingSources()
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

        if message.kind is MessageKind.ATTACHMENT and is_audio(message.attachment):
            await self._async_answer_voice(message, chat)
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

        await self._async_deliver(
            chat,
            reply,
            chat_id=message.chat_id,
            message_id=message.message_id,
            thread_id=message.thread_id,
        )

    async def _async_answer_voice(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Transcribe a voice message with the pipeline and answer it."""
        attachment = message.attachment
        if attachment is None:
            return
        if self._pipeline is None or not self._pipeline.stt_engine:
            await self._reply(message, self._text(chat, "voice_no_stt"))
            return
        if (
            attachment.file_size is not None
            and attachment.file_size > VOICE_DOWNLOAD_LIMIT
        ):
            await self._reply(message, self._text(chat, "voice_too_big"))
            return

        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )

        suffix = audio_suffix(attachment) or ".ogg"
        identifier = message.message_id or uuid.uuid4().hex
        file_name = f"hk_{message.chat_id}_{identifier}{suffix}"
        downloaded = await self._transport.async_download_file(
            attachment.file_id, file_name=file_name
        )
        if downloaded is None:
            await self._reply(message, self._text(chat, "failed"))
            return

        try:
            wav = await async_transcode_to_wav(
                self.hass,
                Path(downloaded),
                max_seconds=self._max_voice_seconds,
            )
        except VoiceTooLongError:
            await self._reply(
                message,
                self._text(chat, "voice_too_long").format(
                    seconds=self._max_voice_seconds
                ),
            )
            return
        except HomeAssistantError as err:
            # The error may name the file, never the audio content.
            _LOGGER.error(
                "Could not decode a voice message in chat %s (%s)",
                message.chat_id,
                type(err).__name__,
            )
            await self._reply(message, self._text(chat, "failed"))
            return
        finally:
            await async_discard_file(self.hass, Path(downloaded))

        try:
            result = await async_run_audio_pipeline(
                self.hass,
                pipeline=self._pipeline,
                conversation_id=chat.conversation_id,
                wav=wav,
                language=self._language(chat),
            )
        except TimeoutError:
            await self._reply(message, self._text(chat, "timeout"))
            return
        except HomeAssistantError as err:
            _LOGGER.error(
                "Voice pipeline failed in chat %s (%s)",
                message.chat_id,
                getattr(err, "code", type(err).__name__),
            )
            await self._reply(message, self._text(chat, "failed"))
            return

        if result.conversation_id and result.conversation_id != chat.conversation_id:
            chat.conversation_id = result.conversation_id
            chat.last_agent = self._agent_id()
            await self._chats.async_save()

        if not result.text.strip():
            await self._reply(message, self._text(chat, "empty"))
            return

        await self._async_deliver(
            chat,
            result.text,
            chat_id=message.chat_id,
            message_id=message.message_id,
            thread_id=message.thread_id,
        )

    async def _async_deliver(
        self,
        chat: ChatContext,
        text: str,
        *,
        chat_id: int,
        message_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        """Send an agent answer as text, voice or both."""
        mode = chat.reply_mode or self._default_reply_mode
        reply_to = message_id if message_id is not None and chat_id < 0 else None
        text_sent = False
        if mode == REPLY_MODE_BOTH:
            # Text first: it arrives while the speech is still being generated.
            await self._transport.async_send_text(
                chat_id, text, thread_id=thread_id, reply_to=reply_to
            )
            text_sent = True

        if mode == REPLY_MODE_TEXT or self._pipeline is None:
            if not text_sent:
                await self._transport.async_send_text(
                    chat_id, text, thread_id=thread_id, reply_to=reply_to
                )
            return
        if not self._pipeline.tts_engine:
            if not text_sent:
                await self._transport.async_send_text(
                    chat_id, text, thread_id=thread_id, reply_to=reply_to
                )
            return

        await self._transport.async_send_chat_action(
            chat_id, "record_voice", thread_id=thread_id
        )

        staged: Path | None = None
        try:
            staged = await async_stage_voice_reply(self.hass, self._pipeline, text)
        except TimeoutError:
            _LOGGER.error("Text-to-speech timed out in chat %s", chat_id)
        except HomeAssistantError as err:
            _LOGGER.error(
                "Text-to-speech failed in chat %s (%s)",
                chat_id,
                type(err).__name__,
            )

        if staged is None:
            # Text is always better than silence, whatever the mode was.
            if not text_sent:
                await self._transport.async_send_text(
                    chat_id, text, thread_id=thread_id, reply_to=reply_to
                )
            return

        try:
            sent = await self._transport.async_send_voice(
                chat_id,
                str(staged),
                thread_id=thread_id,
                reply_to=reply_to,
            )
        finally:
            await async_discard_file(self.hass, staged)

        if not sent and not text_sent:
            await self._transport.async_send_text(
                chat_id, text, thread_id=thread_id, reply_to=reply_to
            )

    async def async_send_briefing(self, chat_id: int | None = None) -> None:
        """Build and deliver the briefing to one chat or every private chat."""
        if chat_id is not None:
            targets = [chat_id]
        else:
            targets = [
                known.chat_id
                for known in self._chats.chats.values()
                if known.chat_id > 0
            ]
        if not targets:
            _LOGGER.debug("No private chat is known yet, skipping the briefing")
            return

        for target in targets:
            chat = self._chats.get(target)
            text = await async_build_briefing(
                self.hass, self._briefing, self._language(chat)
            )
            await self._async_deliver(chat, text, chat_id=target)

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
            await self._async_briefing_command(message, chat)
        elif command == "model":
            await self._reply(message, self._text(chat, "model_soon"))
        else:
            # Another automation may own this command (for example /shop or
            # /video). Staying quiet keeps both working in the same chat.
            _LOGGER.debug("Ignoring command %s: not ours", command)

    async def _async_briefing_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Build the briefing and send it to this chat."""
        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )
        text = await async_build_briefing(
            self.hass, self._briefing, self._language(chat)
        )
        await self._reply(message, text)

    async def _async_voice_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Switch the reply mode for this chat."""
        argument = message.args[0].lower() if message.args else ""
        new_mode = _VOICE_MODES.get(argument)
        if new_mode is None:
            await self._reply(message, self._text(chat, "voice_unknown"))
            return

        if new_mode != REPLY_MODE_TEXT and (
            self._pipeline is None or not self._pipeline.tts_engine
        ):
            await self._reply(message, self._text(chat, "voice_no_tts"))
            return

        chat.reply_mode = new_mode
        await self._chats.async_save()
        await self._reply(message, self._text(chat, "voice_set").format(mode=new_mode))

    async def _reply(self, message: IncomingMessage, text: str) -> None:
        """Send a plain text reply."""
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
