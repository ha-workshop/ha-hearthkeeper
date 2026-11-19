"""Conversation brain: commands, routing and reply modes."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

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
from .mail import Mailbox, async_mail_digest
from .reminders import (
    ReminderManager,
    ReminderRequest,
    looks_like_reminder,
    parse_reminder,
    parse_reminder_command,
)
from .shop import ShopSkill
from .skills import SkillRouter
from .stats import RuntimeStats
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
            "/mail — что в почте\n"
            "/remind 20m текст — напоминание\n"
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
        "model_header": "Выберите агента разговора для этого чата.",
        "model_current": "Сейчас: {name}",
        "model_default": "Как в пайплайне",
        "model_set": "Агент: {name}. Начал новый разговор.",
        "model_reset": "Вернул агента из пайплайна. Начал новый разговор.",
        "model_none": (
            "Не нашёл агентов conversation. Проверьте интеграции в Home Assistant."
        ),
        "model_unknown": (
            "Не понял, какого агента включить. Откройте /model и выберите кнопкой."
        ),
        "model_stale": "Список устарел, откройте /model заново.",
        "attachment_soon": (
            "Пока понимаю текст и голосовые сообщения — отправьте текст или голосовое."
        ),
        "echo": "Эхо: {text}",
        "mail_not_configured": (
            "Почта не настроена: выберите сенсоры IMAP в настройках Hearthkeeper."
        ),
        "reminder_set": "⏰ Напомню {when}: {text}",
        "reminder_usage": (
            "Не понял время. Напишите, например: /remind 20m выключить духовку, "
            "/remind 20:30 позвонить, или «напомни через 20 минут …»."
        ),
        "reminder_empty": "Активных напоминаний нет.",
        "reminder_header": "Активные напоминания:",
        "reminder_item": "• {when} — {text}",
        "reminder_cancelled": "Отменил напоминаний: {count}.",
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
            "/mail — what is in the mail\n"
            "/remind 20m text — a reminder\n"
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
        "model_header": "Choose the conversation agent for this chat.",
        "model_current": "Current: {name}",
        "model_default": "As in the pipeline",
        "model_set": "Agent: {name}. Started a new conversation.",
        "model_reset": "Back to the pipeline agent. Started a new conversation.",
        "model_none": (
            "No conversation agents found. Check the integrations in Home Assistant."
        ),
        "model_unknown": "Unknown agent. Open /model and pick one with a button.",
        "model_stale": "This list is stale, open /model again.",
        "attachment_soon": (
            "I understand text and voice messages so far — send text or a voice note."
        ),
        "echo": "Echo: {text}",
        "mail_not_configured": (
            "Mail is not configured: pick IMAP sensors in the Hearthkeeper options."
        ),
        "reminder_set": "⏰ I will remind you at {when}: {text}",
        "reminder_usage": (
            "I could not read the time. Try /remind 20m turn off the oven, "
            "/remind 20:30 call mom, or “remind me in 20 minutes to …”."
        ),
        "reminder_empty": "There are no active reminders.",
        "reminder_header": "Active reminders:",
        "reminder_item": "• {when} — {text}",
        "reminder_cancelled": "Cancelled reminders: {count}.",
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

MODEL_CALLBACK = "hk_agent:"

_DEFAULT_AGENT_WORDS = frozenset(("default", "pipeline", "пайплайн", "по умолчанию"))


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
        stats: RuntimeStats | None = None,
        mailbox: Mailbox | None = None,
        allow_cloud_for_mail: bool = False,
        skills: SkillRouter | None = None,
    ) -> None:
        """Initialise the brain with entry-level defaults."""
        self.hass = hass
        self._transport = transport
        self._chats = chats
        self._pipeline = pipeline
        self._default_reply_mode = reply_mode
        self._max_voice_seconds = max_voice_seconds
        self._briefing = briefing or BriefingSources()
        self._stats = stats or RuntimeStats()
        self._mailbox = mailbox
        self._allow_cloud_for_mail = allow_cloud_for_mail
        self._shop = ShopSkill(hass, transport, todo_entity)
        self._skills = skills or SkillRouter(
            hass, self._briefing, mailbox, allow_cloud_for_mail
        )
        self._reminders = ReminderManager(hass, self._async_deliver_reminder)
        # The callback data of an agent button is only an index; the list it
        # points at lives here so long entity ids never hit Telegram's limit.
        self._agent_choices: dict[int, tuple[str, ...]] = {}

    async def async_handle(self, message: IncomingMessage) -> None:
        """Process one accepted message."""
        chat = self._chats.mark_seen(message.chat_id)

        if message.kind is MessageKind.CALLBACK:
            self._stats.callbacks += 1
            language = self._language(chat)
            if await self._shop.async_handle_callback(message, language):
                return
            await self._async_handle_agent_callback(message, chat)
            return

        if message.kind is MessageKind.COMMAND:
            self._stats.commands += 1
            await self._async_handle_command(message, chat)
            return

        if message.kind is MessageKind.ATTACHMENT and is_audio(message.attachment):
            self._stats.voice_in += 1
            await self._async_answer_voice(message, chat)
            return

        if message.kind is MessageKind.ATTACHMENT and not message.text.strip():
            await self._reply(message, self._text(chat, "attachment_soon"))
            return
        # A caption is plain text, so it can go to the agent as is.

        self._stats.text += 1
        await self._async_answer(message, chat)

    async def _async_answer(self, message: IncomingMessage, chat: ChatContext) -> None:
        """Send the user text to the conversation agent."""
        text = strip_bot_mention(message.text, self._transport.bot_username)
        if not text:
            return

        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )

        language = self._language(chat)
        if looks_like_reminder(text, language):
            request = parse_reminder(text, language)
            if request is None:
                await self._reply(message, self._text(chat, "reminder_usage"))
                return
            await self._async_add_reminder(message, chat, request)
            return

        skill = self._skills.match(text, language)
        if skill is not None:
            self._stats.skills += 1
            try:
                answer = await self._skills.async_run(skill, language)
            except HomeAssistantError as err:
                self._stats.record_error(type(err).__name__)
                _LOGGER.error(
                    "Skill %s failed in chat %s (%s)",
                    skill.intent,
                    message.chat_id,
                    type(err).__name__,
                )
                await self._reply(message, self._text(chat, "failed"))
                return
            await self._async_deliver(
                chat,
                answer,
                chat_id=message.chat_id,
                message_id=message.message_id,
                thread_id=message.thread_id,
            )
            return

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
            self._stats.record_error("TimeoutError")
            await self._reply(message, self._text(chat, "timeout"))
            return
        except HomeAssistantError as err:
            # The error text may quote the user message, so log the type only.
            self._stats.record_error(type(err).__name__)
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
            self._stats.record_error("DownloadError")
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
            self._stats.record_error(type(err).__name__)
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
            self._stats.record_error("TimeoutError")
            await self._reply(message, self._text(chat, "timeout"))
            return
        except HomeAssistantError as err:
            self._stats.record_error(type(err).__name__)
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
        mode: str | None = None,
    ) -> None:
        """Send an agent answer as text, voice or both."""
        mode = mode or chat.reply_mode or self._default_reply_mode
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
            self._stats.record_error("TimeoutError")
            _LOGGER.error("Text-to-speech timed out in chat %s", chat_id)
        except HomeAssistantError as err:
            self._stats.record_error(type(err).__name__)
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
        elif sent:
            self._stats.voice_out += 1

    async def async_say(self, chat_id: int, text: str, mode: str | None = None) -> None:
        """Send a text or voice message to a chat, for the ``say`` service."""
        chat = self._chats.get(chat_id)
        await self._async_deliver(chat, text, chat_id=chat_id, mode=mode)

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

        self._stats.briefings += 1
        for target in targets:
            chat = self._chats.get(target)
            text = await async_build_briefing(
                self.hass,
                self._briefing,
                self._language(chat),
                mailbox=self._mailbox,
                allow_cloud_for_mail=self._allow_cloud_for_mail,
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
        elif command == "mail":
            await self._async_mail_command(message, chat)
        elif command in ("remind", "reminder"):
            await self._async_reminder_command(message, chat)
        elif command == "model":
            await self._async_model_command(message, chat)
        else:
            # Another automation may own this command (for example /shop or
            # /video). Staying quiet keeps both working in the same chat.
            _LOGGER.debug("Ignoring command %s: not ours", command)

    async def _async_model_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """List conversation agents or switch to one by name."""
        argument = " ".join(message.args).strip()
        if argument:
            await self._async_switch_agent(message, chat, argument)
            return

        agents = self._agent_list()
        if not agents:
            await self._reply(message, self._text(chat, "model_none"))
            return

        self._agent_choices[message.chat_id] = tuple(agent_id for agent_id, _ in agents)
        current = chat.agent_id or self._agent_id()
        keyboard: list[list[list[str]]] = [
            [
                [
                    f"{'✅ ' if agent_id == current else ''}{name}",
                    f"{MODEL_CALLBACK}{index}",
                ]
            ]
            for index, (agent_id, name) in enumerate(agents)
        ]
        keyboard.append(
            [
                [
                    f"↩️ {self._text(chat, 'model_default')}",
                    f"{MODEL_CALLBACK}default",
                ]
            ]
        )

        text = "\n".join(
            (
                self._text(chat, "model_header"),
                self._text(chat, "model_current").format(
                    name=self._agent_name(current) or self._text(chat, "model_default")
                ),
            )
        )
        await self._transport.async_send_text(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id if message.chat_id < 0 else None,
            keyboard=keyboard,
        )

    async def _async_switch_agent(
        self, message: IncomingMessage, chat: ChatContext, argument: str
    ) -> None:
        """Switch the agent from a text argument."""
        if argument.casefold() in _DEFAULT_AGENT_WORDS:
            await self._async_apply_agent(chat, None)
            await self._reply(message, self._text(chat, "model_reset"))
            return

        wanted = argument.casefold()
        match = None
        for agent_id, name in self._agent_list():
            aliases = {
                agent_id.casefold(),
                agent_id.split(".", 1)[-1].casefold(),
                name.casefold(),
            }
            if wanted in aliases:
                match = agent_id
                break
        if match is None:
            await self._reply(message, self._text(chat, "model_unknown"))
            return

        await self._async_apply_agent(chat, match)
        await self._reply(
            message,
            self._text(chat, "model_set").format(name=self._agent_name(match) or match),
        )

    async def _async_handle_agent_callback(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Handle a tap on an agent button."""
        data = message.callback_data or ""
        if not data.startswith(MODEL_CALLBACK):
            return

        texts = self._table(chat)
        key = data[len(MODEL_CALLBACK) :]
        if key == "default":
            agent_id: str | None = None
        else:
            try:
                index = int(key)
            except ValueError:
                index = -1
            choices = self._agent_choices.get(message.chat_id, ())
            if not 0 <= index < len(choices):
                await self._transport.async_answer_callback(
                    message.callback_id, texts["model_stale"]
                )
                return
            agent_id = choices[index]

        await self._transport.async_answer_callback(message.callback_id)
        await self._async_apply_agent(chat, agent_id)
        if message.message_id is None:
            return

        if agent_id is None:
            text = texts["model_reset"]
        else:
            text = texts["model_set"].format(
                name=self._agent_name(agent_id) or agent_id
            )
        await self._transport.async_edit_message(
            message.chat_id, message.message_id, text
        )

    async def _async_apply_agent(self, chat: ChatContext, agent_id: str | None) -> None:
        """Remember the agent for this chat and start a fresh conversation."""
        chat.agent_id = agent_id
        chat.conversation_id = f"tg:{chat.chat_id}"
        chat.last_agent = agent_id
        await self._chats.async_save()

    def _agent_list(self) -> list[tuple[str, str]]:
        """Return available conversation agents as ``(entity_id, name)``."""
        agents: list[tuple[str, str]] = []
        for state in self.hass.states.async_all("conversation"):
            if state.state == "unavailable":
                continue
            name = state.attributes.get("friendly_name")
            agents.append(
                (
                    state.entity_id,
                    name if isinstance(name, str) and name else state.entity_id,
                )
            )
        agents.sort(key=lambda item: item[1].casefold())
        return agents

    def _agent_name(self, agent_id: str | None) -> str | None:
        """Return a display name of an agent."""
        if not agent_id:
            return None
        state = self.hass.states.get(agent_id)
        if state is None:
            return agent_id
        name = state.attributes.get("friendly_name")
        return name if isinstance(name, str) and name else agent_id

    async def _async_briefing_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Build the briefing and send it to this chat."""
        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )
        text = await async_build_briefing(
            self.hass,
            self._briefing,
            self._language(chat),
            mailbox=self._mailbox,
            allow_cloud_for_mail=self._allow_cloud_for_mail,
        )
        await self._reply(message, text)

    async def _async_mail_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Show the mail digest for this chat."""
        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )
        text = await async_mail_digest(
            self.hass,
            self._briefing,
            self._language(chat),
            mailbox=self._mailbox,
            allow_cloud=self._allow_cloud_for_mail,
        )
        await self._reply(message, text or self._text(chat, "mail_not_configured"))

    async def _async_reminder_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Add, list or cancel reminders for this chat."""
        language = self._language(chat)
        argument = " ".join(message.args).strip()
        lowered = argument.lower()

        if lowered in ("list", "список"):
            await self._async_list_reminders(message, chat)
            return
        if lowered in ("cancel", "clear", "отмена", "отмени"):
            count = self._reminders.cancel_all(message.chat_id)
            await self._reply(
                message, self._text(chat, "reminder_cancelled").format(count=count)
            )
            return

        request = parse_reminder_command(argument, language)
        if request is None:
            await self._reply(message, self._text(chat, "reminder_usage"))
            return
        await self._async_add_reminder(message, chat, request)

    async def _async_list_reminders(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Send the active reminders of this chat."""
        items = self._reminders.items(message.chat_id)
        if not items:
            await self._reply(message, self._text(chat, "reminder_empty"))
            return
        lines = [self._text(chat, "reminder_header")]
        lines.extend(
            self._text(chat, "reminder_item").format(
                when=_format_due(item.due), text=item.text
            )
            for item in items
        )
        await self._reply(message, "\n".join(lines))

    async def _async_add_reminder(
        self, message: IncomingMessage, chat: ChatContext, request: ReminderRequest
    ) -> None:
        """Schedule one reminder and confirm it with the local time."""
        error = self._reminders.async_add(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            text=request.text,
            due=request.due,
            language=self._language(chat),
        )
        if error is not None:
            await self._reply(message, error)
            return
        self._stats.reminders += 1
        await self._reply(
            message,
            self._text(chat, "reminder_set").format(
                when=_format_due(request.due), text=request.text
            ),
        )

    async def _async_deliver_reminder(
        self, chat_id: int, thread_id: int | None, text: str
    ) -> None:
        """Send a due reminder to its chat."""
        await self._transport.async_send_text(chat_id, text, thread_id=thread_id)

    def async_stop(self) -> None:
        """Cancel pending reminders when the config entry unloads."""
        self._reminders.async_stop()

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

    def _table(self, chat: ChatContext) -> dict[str, str]:
        """Return the localised string table of a chat."""
        language = self._language(chat)
        return TEXTS.get(language, TEXTS[DEFAULT_LANGUAGE])

    def _text(self, chat: ChatContext, key: str) -> str:
        """Return a localised chat string."""
        return self._table(chat)[key]


__all__ = ["MODEL_CALLBACK", "Brain", "extract_speech"]


def _format_due(due: datetime) -> str:
    """Format a reminder time in the local timezone."""
    return dt_util.as_local(due).strftime("%d.%m %H:%M")
