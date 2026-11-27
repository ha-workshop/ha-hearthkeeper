"""Conversation brain: commands, routing and reply modes."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .assist import PipelineSettings, extract_speech
from .brief import (
    BriefingSources,
    async_build_briefing,
    async_events_between,
    is_quiet_hours,
)
from .connections import ConnectionPlugins
from .const import (
    BRIEFING_MAIL,
    CONVERSATION_TIMEOUT,
    DEFAULT_LANGUAGE,
    DEFAULT_MAX_VOICE_SECONDS,
    REPLY_MODE_BOTH,
    REPLY_MODE_TEXT,
    REPLY_MODE_VOICE,
    REPLY_MODES,
    VOICE_DOWNLOAD_LIMIT,
)
from .mail import Mailbox, async_mail_digest, async_personal_mail_digest
from .personal_calendar import CalendarError, PersonalCalendars
from .personal_mail import MailboxError, PersonalMail, validate
from .reminders import (
    ReminderManager,
    ReminderRequest,
    looks_like_reminder,
    parse_reminder,
    parse_reminder_command,
)
from .settings import apply_preferences
from .shop import ShopSkill
from .skills import INTENT_BRIEF, INTENT_MAIL, SkillRouter
from .stats import RuntimeStats
from .store import ChatContext, ChatStore, normalise_time
from .transport import IncomingMessage, MessageKind, Transport, strip_bot_mention
from .voice import (
    VoiceTooLongError,
    async_discard_file,
    async_run_audio_pipeline,
    async_stage_voice_reply,
    async_transcode_and_maybe_keep,
    audio_suffix,
    is_audio,
)
from .webapp import get_webapp_url

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
            "/web — открыть Mini App\n"
            "/model — сменить агента\n"
            "/web — открыть приложение\n"
            "/help — эта справка"
        ),
        "reset_done": "Контекст разговора сброшен.",
        "web_open": "Настройки и быстрые действия — в приложении:",
        "web_button": "🏠 Открыть Hearthkeeper",
        "web_no_url": (
            "Приложению нужен HTTPS-адрес Home Assistant. Укажите внешний URL "
            "(Nabu Casa или свой прокси) в настройках сети HA."
        ),
        "web_private_only": "Приложение открывается только в личном чате с ботом.",
        "web_failed": (
            "Не получилось отправить кнопку приложения. Подробности в логе HA."
        ),
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
        "skill_no_agent": (
            "Для навыка «{name}» нужен агент conversation. Выберите его в /model."
        ),
        "event_soon": "⏰ Через {minutes} мин, в {time}: {summary}",
        "birthdays": "🎂 Сегодня день рождения:",
        "birthday_prompt": (
            "Напиши одно короткое тёплое поздравление с днём рождения "
            "(2–3 предложения), которое можно отправить. Повод: {who}. "
            "Без вступления и вариантов."
        ),
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
            "/web — open the Mini App\n"
            "/model — switch the agent\n"
            "/web — open the app\n"
            "/help — this help"
        ),
        "reset_done": "Conversation context cleared.",
        "web_open": "Settings and quick actions live in the app:",
        "web_button": "🏠 Open Hearthkeeper",
        "web_no_url": (
            "The app needs an HTTPS address of Home Assistant. Set the external "
            "URL (Nabu Casa or your own proxy) in the HA network settings."
        ),
        "web_private_only": "The app opens only in a private chat with the bot.",
        "web_failed": "Could not send the app button. See the Home Assistant log.",
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
        "skill_no_agent": (
            "The «{name}» skill needs a conversation agent. Pick one with /model."
        ),
        "event_soon": "⏰ In {minutes} min, at {time}: {summary}",
        "birthdays": "🎂 Birthdays today:",
        "birthday_prompt": (
            "Write one short, warm birthday greeting (2–3 sentences) ready to "
            "send. Occasion: {who}. No introduction and no options."
        ),
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
        keep_audio: bool = False,
        quiet_start: str = "",
        quiet_end: str = "",
        skills: SkillRouter | None = None,
        brief_enabled: bool = False,
        brief_time: str = "",
        brief_chats: tuple[int, ...] = (),
        webapp_url: str | None = None,
        connections: ConnectionPlugins | None = None,
        personal_mail: PersonalMail | None = None,
        personal_calendars: PersonalCalendars | None = None,
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
        self._keep_audio = keep_audio
        self._quiet_start = quiet_start
        self._quiet_end = quiet_end
        self._todo_entity = todo_entity
        self._brief_enabled = brief_enabled
        self._brief_time = brief_time
        self._brief_chats = brief_chats
        self._webapp_url = webapp_url
        self._connections = connections
        self._personal_mail = personal_mail
        self._personal_calendars = personal_calendars
        self._shop = ShopSkill(hass, transport, todo_entity)
        self._skills = skills or SkillRouter(
            hass, self._briefing, mailbox, allow_cloud_for_mail
        )
        self._reminders = ReminderManager(hass, self._async_deliver_reminder)
        # The callback data of an agent button is only an index; the list it
        # points at lives here so long entity ids never hit Telegram's limit.
        self._agent_choices: dict[int, tuple[str, ...]] = {}
        # Event reminders already sent: ``(chat_id, start, summary)``.
        self._events_sent: set[tuple[int, datetime, str]] = set()

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

        if message.kind is MessageKind.WEB_APP:
            # Data sent by a Mini App is a command line, never a question for
            # the agent.
            parts = message.text.split()
            if not parts or not parts[0].startswith("/"):
                return
            self._stats.commands += 1
            await self._async_handle_command(
                replace(
                    message,
                    kind=MessageKind.COMMAND,
                    command=parts[0],
                    args=tuple(parts[1:]),
                ),
                chat,
            )
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
                answer = await self._skills.async_run(
                    skill,
                    language,
                    sources=self._sources(chat),
                    mail_text=(
                        await self._async_personal_mail_text(chat)
                        if skill.intent in (INTENT_MAIL, INTENT_BRIEF)
                        else None
                    ),
                )
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

        removed = False
        try:
            wav, removed = await async_transcode_and_maybe_keep(
                self.hass,
                Path(downloaded),
                max_seconds=self._max_voice_seconds,
                keep=self._keep_audio,
            )
        except VoiceTooLongError:
            await async_discard_file(self.hass, Path(downloaded))
            await self._reply(
                message,
                self._text(chat, "voice_too_long").format(
                    seconds=self._max_voice_seconds
                ),
            )
            return
        except HomeAssistantError as err:
            await async_discard_file(self.hass, Path(downloaded))
            self._stats.record_error(type(err).__name__)
            _LOGGER.error(
                "Could not decode a voice message in chat %s (%s)",
                message.chat_id,
                type(err).__name__,
            )
            await self._reply(message, self._text(chat, "failed"))
            return
        if not removed:
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

    async def async_send_briefing(
        self,
        chat_id: int | None = None,
        *,
        brief_chats: tuple[int, ...] = (),
        scheduled: bool = False,
        kind: str = "",
    ) -> None:
        """Build and deliver the briefing to one chat or every private chat.

        ``kind`` can be ``"morning"`` (plan for the day) or ``"evening"``
        (what happened today). When empty the briefing shows everything as
        before.
        """
        if chat_id is not None:
            targets = [chat_id]
        elif brief_chats or self._brief_chats:
            wanted = brief_chats or self._brief_chats
            targets = [cid for cid in wanted if cid in self._chats.chats]
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
            # Quiet hours hold back only the daily schedule; a briefing
            # someone asked for right now is always sent.
            if scheduled and self._is_quiet(chat):
                _LOGGER.debug("Quiet hours: skipping a scheduled briefing")
                continue
            if kind == BRIEFING_MAIL:
                text = await self._async_mail_text(chat)
                if not text:
                    _LOGGER.debug("No mailbox for chat %s, skipping the digest", target)
                    continue
            else:
                text = await async_build_briefing(
                    self.hass,
                    self._sources(chat),
                    self._language(chat),
                    mailbox=self._mailbox,
                    allow_cloud_for_mail=self._allow_cloud_for_mail,
                    kind=kind or None,
                    extra_lines=self._plugin_lines(chat),
                    personal_mail=await self._async_personal_mail_text(chat),
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
        elif command == "web":
            await self._async_web_command(message, chat)
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
            self._sources(chat),
            self._language(chat),
            mailbox=self._mailbox,
            allow_cloud_for_mail=self._allow_cloud_for_mail,
            extra_lines=self._plugin_lines(chat),
            personal_mail=await self._async_personal_mail_text(chat),
        )
        await self._reply(message, text)

    async def _async_mail_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Show the mail digest for this chat."""
        await self._transport.async_send_chat_action(
            message.chat_id, "typing", thread_id=message.thread_id
        )
        text = await self._async_mail_text(chat)
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
        """Send a due reminder; during quiet hours it arrives without a sound."""
        silent = self._is_quiet(self._chats.get(chat_id))
        await self._transport.async_send_text(
            chat_id, text, thread_id=thread_id, silent=silent
        )

    # Personal settings ----------------------------------------------------

    def _sources(self, chat: ChatContext) -> BriefingSources:
        """Return the briefing sources of a chat.

        Home Assistant calendars and mailboxes are limited to the ones the
        integration options share, even if the options changed since. A
        user's own calendars come from their iCal links.
        """
        override = chat.sources or {}
        shared = self._briefing
        return BriefingSources(
            calendars=tuple(
                item
                for item in override.get("calendars", shared.calendars)
                if item in shared.calendars
            ),
            weather=override.get("weather", shared.weather),
            todo=shared.todo,
            mail=tuple(
                item
                for item in override.get("mail", shared.mail)
                if item in shared.mail
            ),
            home=override.get("home", shared.home),
            ics=(
                self._personal_calendars.urls(chat.chat_id)
                if self._personal_calendars
                else ()
            ),
        )

    def _quiet_hours(self, chat: ChatContext) -> tuple[str, str]:
        """Return the quiet interval of a chat; empty strings mean off."""
        if chat.quiet is not None:
            return (chat.quiet[0], chat.quiet[1]) if chat.quiet else ("", "")
        return (
            normalise_time(self._quiet_start) or "",
            normalise_time(self._quiet_end) or "",
        )

    def _is_quiet(self, chat: ChatContext) -> bool:
        start, end = self._quiet_hours(chat)
        return is_quiet_hours(quiet_start=start, quiet_end=end)

    def _brief_schedule(self, chat: ChatContext) -> list[tuple[str, str]]:
        """Return ``[(kind, time), …]`` for every enabled briefing of a chat.

        Falls back to the household morning briefing with the old options.
        """
        plans = chat.briefings
        if plans is not None:
            return [(plan.kind, plan.time) for plan in plans if plan.enabled]
        # Fallback: the household defaults from the integration options.
        if self._brief_enabled:
            time = normalise_time(self._brief_time) or "08:00"
            if not self._brief_chats or chat.chat_id in self._brief_chats:
                return [("morning", time)]
        return []

    async def async_scheduled_tick(self, now: datetime) -> None:
        """Send briefings and custom skills whose time is now."""
        current = f"{now.hour:02d}:{now.minute:02d}"
        due: list[tuple[int, str]] = []
        due_skills: list[tuple[int, str]] = []
        for chat in list(self._chats.chats.values()):
            if chat.chat_id <= 0:
                continue
            for kind, at in self._brief_schedule(chat):
                if at == current:
                    due.append((chat.chat_id, kind))
            for skill in chat.custom_skills:
                if (
                    skill.enabled
                    and skill.time == current
                    and skill.runs_on(now.weekday())
                ):
                    due_skills.append((chat.chat_id, skill.id))
        for chat_id, kind in due:
            await self.async_send_briefing(chat_id, scheduled=True, kind=kind)
        for chat_id, skill_id in due_skills:
            await self.async_run_custom_skill(chat_id, skill_id, scheduled=True)
        await self._async_calendar_tick(now, current)

    async def _async_calendar_tick(self, now: datetime, current: str) -> None:
        """Remind about events starting soon and announce today's birthdays."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt_util.get_default_time_zone())
        minute = now.replace(second=0, microsecond=0)
        self._events_sent = {key for key in self._events_sent if key[1] >= minute}
        for chat in list(self._chats.chats.values()):
            if chat.chat_id <= 0:
                continue
            sources = self._sources(chat)
            if not sources.has_calendars:
                continue
            if chat.event_reminder:
                await self._async_event_reminders(chat, sources, minute)
            if chat.birthdays and chat.birthdays == current:
                await self._async_birthdays(chat, sources, minute)

    async def _async_event_reminders(
        self, chat: ChatContext, sources: BriefingSources, minute: datetime
    ) -> None:
        start = minute + timedelta(minutes=chat.event_reminder)
        events = await async_events_between(
            self.hass, sources, start, start + timedelta(minutes=1)
        )
        for begins, summary, all_day in events:
            key = (chat.chat_id, begins, summary)
            if all_day or key in self._events_sent:
                continue
            self._events_sent.add(key)
            text = self._text(chat, "event_soon").format(
                minutes=chat.event_reminder,
                time=dt_util.as_local(begins).strftime("%H:%M"),
                summary=summary,
            )
            await self._transport.async_send_text(
                chat.chat_id, text, silent=self._is_quiet(chat)
            )

    async def _async_birthdays(
        self, chat: ChatContext, sources: BriefingSources, minute: datetime
    ) -> None:
        day = dt_util.start_of_local_day(minute)
        events = await async_events_between(
            self.hass, sources, day, day + timedelta(days=1)
        )
        people = list(
            dict.fromkeys(
                summary for _, summary, _ in events if _BIRTHDAY.search(summary)
            )
        )
        if not people:
            return
        lines = [self._text(chat, "birthdays"), *(f"• {who}" for who in people)]
        greeting = await self._async_ask_agent(
            chat,
            self._text(chat, "birthday_prompt").format(who="; ".join(people)),
            f"tg:{chat.chat_id}:birthdays",
            report=False,
        )
        if greeting:
            lines += ["", greeting]
        await self._async_deliver(chat, "\n".join(lines), chat_id=chat.chat_id)

    async def _async_ask_agent(
        self,
        chat: ChatContext,
        text: str,
        conversation_id: str,
        *,
        report: bool = True,
    ) -> str | None:
        """Ask the chat's agent once; with ``report`` tell the chat what failed."""
        agent_id = chat.agent_id or self._agent_id()
        if not agent_id:
            return None
        chat_id = chat.chat_id
        try:
            async with asyncio.timeout(CONVERSATION_TIMEOUT):
                result = await self.hass.services.async_call(
                    "conversation",
                    "process",
                    {
                        "text": text,
                        "agent_id": agent_id,
                        "conversation_id": conversation_id,
                        "language": self._conversation_language(chat),
                    },
                    blocking=True,
                    return_response=True,
                )
        except TimeoutError:
            self._stats.record_error("TimeoutError")
            if report:
                await self._transport.async_send_text(
                    chat_id, self._text(chat, "timeout")
                )
            return None
        except HomeAssistantError as err:
            self._stats.record_error(type(err).__name__)
            _LOGGER.error(
                "Agent request failed in chat %s (%s)", chat_id, type(err).__name__
            )
            if report:
                await self._transport.async_send_text(
                    chat_id, self._text(chat, "failed")
                )
            return None
        reply = extract_speech(result)
        if not reply and report:
            await self._transport.async_send_text(chat_id, self._text(chat, "empty"))
        return reply or None

    async def _async_personal_mail_text(self, chat: ChatContext) -> str | None:
        """Return the digest of the chat's own mailbox, or None without one."""
        box = self._personal_mail.get(chat.chat_id) if self._personal_mail else None
        if box is None or self._personal_mail is None:
            return None
        return await async_personal_mail_digest(
            self._personal_mail,
            box,
            self._language(chat),
            allow_cloud=self._allow_cloud_for_mail,
        )

    async def _async_mail_text(self, chat: ChatContext) -> str | None:
        """Return the chat's mail digest: its own mailbox first, else shared."""
        personal = await self._async_personal_mail_text(chat)
        if personal is not None:
            return personal
        return await async_mail_digest(
            self.hass,
            self._sources(chat),
            self._language(chat),
            mailbox=self._mailbox,
            allow_cloud=self._allow_cloud_for_mail,
        )

    async def async_set_personal_mail(
        self, chat_id: int, raw: object
    ) -> dict[str, Any]:
        """Check and save a user's mailbox; raise ``MailboxError`` if it fails."""
        if self._personal_mail is None:
            raise MailboxError("unavailable")
        box = validate(raw, self._personal_mail.get(chat_id))
        unread = await self._personal_mail.async_set(chat_id, box)
        _LOGGER.info("Chat %s connected a personal mailbox", chat_id)
        return {**box.public(), "unread": unread.count}

    async def async_add_personal_calendar(
        self, chat_id: int, name: object, url: object
    ) -> dict[str, Any]:
        """Check and save a calendar link; raise ``CalendarError`` if it fails."""
        if self._personal_calendars is None:
            raise CalendarError("unavailable")
        item, today = await self._personal_calendars.async_add(chat_id, name, url)
        _LOGGER.info("Chat %s added a personal calendar", chat_id)
        return {**item.public(), "today": today}

    async def async_remove_personal_calendar(
        self, chat_id: int, calendar_id: object
    ) -> None:
        """Forget one calendar link."""
        if self._personal_calendars is not None:
            await self._personal_calendars.async_remove(chat_id, calendar_id)

    async def async_remove_personal_mail(self, chat_id: int) -> None:
        """Forget a user's mailbox and its password."""
        if self._personal_mail is not None:
            await self._personal_mail.async_remove(chat_id)

    def _plugin_lines(self, chat: ChatContext) -> list[str]:
        """Return the briefing lines connection plugins add."""
        if self._connections is None:
            return []
        return self._connections.briefing_lines(self._language(chat))

    def chat_language(self, chat_id: int) -> str:
        """Return the two-letter language the bot uses in a chat."""
        return self._language(self._chats.get(chat_id))

    def has_custom_skill(self, chat_id: int, skill_id: str) -> bool:
        """Return True when the chat has a custom skill with this id."""
        return any(
            skill.id == skill_id for skill in self._chats.get(chat_id).custom_skills
        )

    async def async_run_custom_skill(
        self, chat_id: int, skill_id: str, *, scheduled: bool = False
    ) -> bool:
        """Ask the agent a custom skill's request and send the answer.

        Each skill talks to the agent in its own conversation, so a daily
        request neither mixes with the chat nor sees what was said there.
        """
        chat = self._chats.get(chat_id)
        skill = next((s for s in chat.custom_skills if s.id == skill_id), None)
        if skill is None:
            return False
        if scheduled and self._is_quiet(chat):
            _LOGGER.debug("Quiet hours: skipping a scheduled custom skill")
            return False
        agent_id = chat.agent_id or self._agent_id()
        if not agent_id:
            await self._transport.async_send_text(
                chat_id, self._text(chat, "skill_no_agent").format(name=skill.name)
            )
            return False

        self._stats.skills += 1
        await self._transport.async_send_chat_action(chat_id, "typing")
        reply = await self._async_ask_agent(
            chat, skill.prompt, f"tg:{chat_id}:skill:{skill.id}"
        )
        if not reply:
            return False
        skill.last_run = dt_util.utcnow().isoformat(timespec="seconds")
        await self._chats.async_save()
        await self._async_deliver(chat, f"✨ {skill.name}\n\n{reply}", chat_id=chat_id)
        return True

    def async_stop(self) -> None:
        """Cancel pending reminders when the config entry unloads."""
        self._reminders.async_stop()

    async def _async_web_command(
        self, message: IncomingMessage, chat: ChatContext
    ) -> None:
        """Send a button that opens the Mini App."""
        if message.chat_id < 0:
            await self._reply(message, self._text(chat, "web_private_only"))
            return
        url = get_webapp_url(self.hass, self._webapp_url)
        if url is None:
            await self._reply(message, self._text(chat, "web_no_url"))
            return
        sent = await self._transport.async_send_web_app_button(
            message.chat_id,
            self._text(chat, "web_open"),
            self._text(chat, "web_button"),
            url,
        )
        if not sent:
            await self._reply(message, self._text(chat, "web_failed"))

    # Mini App API -------------------------------------------------------

    def webapp_state(self, chat_id: int, first_name: str = "") -> dict[str, object]:
        """Return what the Mini App shows for one private chat."""
        chat = self._chats.get(chat_id)
        pipeline_agent = self._agent_id()
        current = chat.agent_id or pipeline_agent
        tts = bool(self._pipeline and self._pipeline.tts_engine)
        stt = bool(self._pipeline and self._pipeline.stt_engine)
        sources = self._sources(chat)
        brief_schedule = self._brief_schedule(chat)
        quiet_start, quiet_end = self._quiet_hours(chat)
        return {
            "first_name": first_name,
            "language": self._language(chat),
            "bot_username": self._transport.bot_username,
            "reply_mode": chat.reply_mode or self._default_reply_mode,
            "reply_modes": list(REPLY_MODES),
            "tts": tts,
            "stt": stt,
            "pipeline": self._pipeline.name if self._pipeline else None,
            "agent": {
                "id": current,
                "name": self._agent_name(current),
                "overridden": chat.agent_id is not None,
                "pipeline_name": self._agent_name(pipeline_agent),
            },
            "agents": [
                {"id": agent_id, "name": name}
                for agent_id, name in self._agent_list()
            ],
            "personal": chat.has_prefs,
            "briefings": [
                {"id": plan.id, "kind": plan.kind, "time": plan.time, "name": plan.name, "enabled": plan.enabled}
                for plan in (chat.briefings or [])
            ],
            "brief": {
                "enabled": bool(brief_schedule),
                "time": brief_schedule[0][1] if brief_schedule else "",
                "active": brief_schedule,
                "quiet_start": quiet_start,
                "quiet_end": quiet_end,
                "has_personal_briefings": chat.briefings is not None,
                "personal_quiet": chat.quiet is not None,
            },
            "sources": {
                "weather": self._entity_name(sources.weather),
                "weather_id": sources.weather,
                "calendars": [
                    self._entity_name(entity) for entity in sources.calendars
                ],
                "calendar_ids": list(sources.calendars),
                "todo": self._entity_name(self._todo_entity),
                "mail": bool(sources.mail),
                "mail_ids": list(sources.mail),
                "home": sources.home,
                "personal": bool(chat.sources),
            },
            "choices": {
                "weather": self._entity_choices(self._weather_ids()),
                "calendars": self._entity_choices(self._briefing.calendars),
                "mail": self._entity_choices(self._briefing.mail),
            },
            "shared": {
                "max_voice_seconds": self._max_voice_seconds,
                "keep_audio": self._keep_audio,
                "allow_cloud_for_mail": self._allow_cloud_for_mail,
            },
            "custom_skills": [skill.as_dict() for skill in chat.custom_skills],
            "mascot": {"color": chat.mascot_color, "name": chat.mascot_name},
            "time_zone": self.hass.config.time_zone,
            "personal_calendars": [
                item.public()
                for item in (
                    self._personal_calendars.get(chat_id)
                    if self._personal_calendars
                    else ()
                )
            ],
            "personal_mail": (
                box.public()
                if self._personal_mail
                and (box := self._personal_mail.get(chat_id)) is not None
                else None
            ),
            "calendar_alerts": {
                "event_reminder": chat.event_reminder,
                "birthdays": chat.birthdays,
            },
            "reminders": [
                {"when": _format_due(item.due), "text": item.text}
                for item in self._reminders.items(chat_id)
            ],
        }

    async def async_set_reply_mode(self, chat_id: int, mode: str) -> bool:
        """Change the reply mode of a chat; False when voice is not possible."""
        if mode != REPLY_MODE_TEXT and not (
            self._pipeline and self._pipeline.tts_engine
        ):
            return False
        chat = self._chats.get(chat_id)
        chat.reply_mode = mode
        await self._chats.async_save()
        return True

    async def async_set_agent(self, chat_id: int, agent_id: str | None) -> bool:
        """Pin a conversation agent to a chat, or go back to the pipeline's."""
        if agent_id is not None and agent_id not in {
            known for known, _ in self._agent_list()
        }:
            return False
        await self._async_apply_agent(self._chats.get(chat_id), agent_id)
        return True

    async def async_new_conversation(self, chat_id: int) -> None:
        """Forget the conversation context of a chat."""
        chat = self._chats.get(chat_id)
        chat.conversation_id = f"tg:{chat_id}"
        await self._chats.async_save()

    async def async_update_preferences(
        self, chat_id: int, patch: object
    ) -> list[str]:
        """Change the personal settings of a private chat.

        Raises ``InvalidPreference`` when any value is not acceptable.
        """
        chat = self._chats.get(chat_id)
        changed = apply_preferences(
            chat,
            patch,
            weather_ids=self._weather_ids(),
            calendar_pool=self._briefing.calendars,
            mail_pool=self._briefing.mail,
        )
        await self._chats.async_save()
        return changed

    def _weather_ids(self) -> list[str]:
        """Return every weather entity, the shared one first."""
        ids = [state.entity_id for state in self.hass.states.async_all("weather")]
        if self._briefing.weather and self._briefing.weather not in ids:
            ids.insert(0, self._briefing.weather)
        return ids

    def _entity_choices(self, entity_ids: object) -> list[dict[str, str]]:
        """Return ``{id, name}`` pairs for the page's pickers."""
        return [
            {"id": entity_id, "name": self._entity_name(entity_id) or entity_id}
            for entity_id in entity_ids  # type: ignore[attr-defined]
        ]

    def cancel_reminders(self, chat_id: int) -> int:
        """Cancel every reminder of a chat."""
        return self._reminders.cancel_all(chat_id)

    def _entity_name(self, entity_id: str | None) -> str | None:
        """Return the friendly name of an entity, or its id."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        name = state.attributes.get("friendly_name") if state else None
        return name if isinstance(name, str) and name else entity_id

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

# Calendar entries that mean a birthday: "День рождения Маши", "ДР Пети",
# "Anna's birthday", "🎂 Mom".
_BIRTHDAY = re.compile(r"(день\s+рождени|\bдр\b|birthday|b-?day|🎂)", re.IGNORECASE)


def _format_due(due: datetime) -> str:
    """Format a reminder time in the local timezone."""
    return dt_util.as_local(due).strftime("%d.%m %H:%M")
