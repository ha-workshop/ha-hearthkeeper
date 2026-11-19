"""Hearthkeeper: a Telegram assistant for Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_track_time_change

from .assist import PipelineSettings, async_get_pipeline_settings
from .brain import Brain
from .brief import BriefingSources, parse_brief_time
from .const import (
    ATTR_CHAT_ID,
    ATTR_MODE,
    ATTR_TEXT,
    CONF_ALLOW_CLOUD_FOR_MAIL,
    CONF_ALLOW_GROUPS,
    CONF_BRIEF_CALENDARS,
    CONF_BRIEF_ENABLED,
    CONF_BRIEF_HOME,
    CONF_BRIEF_MAIL,
    CONF_BRIEF_TIME,
    CONF_BRIEF_WEATHER,
    CONF_MAX_VOICE_SECONDS,
    CONF_PIPELINE_ID,
    CONF_REPLY_MODE,
    CONF_TELEGRAM_ENTRY,
    CONF_TODO_ENTITY,
    DEFAULT_ALLOW_CLOUD_FOR_MAIL,
    DEFAULT_ALLOW_GROUPS,
    DEFAULT_BRIEF_ENABLED,
    DEFAULT_BRIEF_HOME,
    DEFAULT_BRIEF_TIME,
    DEFAULT_MAX_VOICE_SECONDS,
    DEFAULT_OPTIONS,
    DEFAULT_REPLY_MODE,
    DOMAIN,
    REPLY_MODE_BOTH,
    REPLY_MODE_TEXT,
    REPLY_MODE_VOICE,
    SERVICE_BRIEF,
    SERVICE_RESET,
    SERVICE_SAY,
    TELEGRAM_DOMAIN,
    TELEGRAM_SUBENTRY_ALLOWED_CHATS,
)
from .mail import Mailbox
from .skills import (
    SkillRouter,
    async_install_sentences,
    async_register_intents,
    async_reload_assist,
    async_remove_intents,
    async_remove_sentences,
)
from .stats import RuntimeStats
from .store import ChatStore
from .transport import Transport

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HearthkeeperRuntime:
    """Objects that live as long as the config entry."""

    telegram_entry_id: str
    allowed_chats: set[int]
    pipeline: PipelineSettings | None
    transport: Transport
    brain: Brain
    chats: ChatStore
    briefing: BriefingSources
    brief_enabled: bool
    brief_time: str
    stats: RuntimeStats

    def handles_chat(self, chat_id: int) -> bool:
        """Return True when the chat belongs to this entry."""
        return not self.allowed_chats or chat_id in self.allowed_chats


type HearthkeeperConfigEntry = ConfigEntry[HearthkeeperRuntime]


async def async_setup_entry(
    hass: HomeAssistant, entry: HearthkeeperConfigEntry
) -> bool:
    """Set up Hearthkeeper from a config entry."""
    telegram_entry_id = entry.data.get(CONF_TELEGRAM_ENTRY)
    telegram_entry = (
        hass.config_entries.async_get_entry(telegram_entry_id)
        if isinstance(telegram_entry_id, str)
        else None
    )
    if telegram_entry is None or telegram_entry.domain != TELEGRAM_DOMAIN:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="telegram_entry_missing"
        )
    if telegram_entry.state is not ConfigEntryState.LOADED:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="telegram_entry_not_loaded"
        )

    allowed_chats = _allowed_chats(telegram_entry)
    if not allowed_chats:
        _LOGGER.warning(
            "No allowed chats found in %s; Hearthkeeper will accept only "
            "messages the core integration already authorised",
            telegram_entry.title,
        )

    options = dict(DEFAULT_OPTIONS) | dict(entry.options)
    pipeline = _resolve_pipeline(hass, options.get(CONF_PIPELINE_ID))
    if pipeline is None:
        _LOGGER.info(
            "No Assist pipeline selected; Hearthkeeper will echo messages back"
        )

    chats = ChatStore(hass)
    await chats.async_load()

    transport = Transport(
        hass,
        telegram_entry_id=telegram_entry.entry_id,
        allowed_chats=allowed_chats,
        allow_groups=bool(options.get(CONF_ALLOW_GROUPS, DEFAULT_ALLOW_GROUPS)),
    )
    briefing = BriefingSources(
        calendars=_str_list(options.get(CONF_BRIEF_CALENDARS)),
        weather=_str_or_none(options.get(CONF_BRIEF_WEATHER)),
        todo=_str_or_none(options.get(CONF_TODO_ENTITY)),
        mail=_str_list(options.get(CONF_BRIEF_MAIL)),
        home=bool(options.get(CONF_BRIEF_HOME, DEFAULT_BRIEF_HOME)),
    )
    stats = RuntimeStats()
    mailbox = Mailbox(hass)
    mailbox.async_start()
    entry.async_on_unload(mailbox.async_stop)
    allow_cloud_for_mail = bool(
        options.get(CONF_ALLOW_CLOUD_FOR_MAIL, DEFAULT_ALLOW_CLOUD_FOR_MAIL)
    )
    skill_router = SkillRouter(
        hass, briefing, mailbox, allow_cloud_for_mail=allow_cloud_for_mail
    )
    brain = Brain(
        hass,
        transport,
        chats,
        pipeline=pipeline,
        reply_mode=_str_or_none(options.get(CONF_REPLY_MODE)) or DEFAULT_REPLY_MODE,
        todo_entity=_str_or_none(options.get(CONF_TODO_ENTITY)),
        max_voice_seconds=(
            _as_int(options.get(CONF_MAX_VOICE_SECONDS)) or DEFAULT_MAX_VOICE_SECONDS
        ),
        briefing=briefing,
        stats=stats,
        mailbox=mailbox,
        allow_cloud_for_mail=allow_cloud_for_mail,
        skills=skill_router,
    )
    transport.set_handler(brain.async_handle)

    runtime = HearthkeeperRuntime(
        telegram_entry_id=telegram_entry.entry_id,
        allowed_chats=allowed_chats,
        pipeline=pipeline,
        transport=transport,
        brain=brain,
        chats=chats,
        briefing=briefing,
        brief_enabled=bool(options.get(CONF_BRIEF_ENABLED, DEFAULT_BRIEF_ENABLED)),
        brief_time=_str_or_none(options.get(CONF_BRIEF_TIME)) or DEFAULT_BRIEF_TIME,
        stats=stats,
    )
    entry.runtime_data = runtime

    skill_router = SkillRouter(hass, briefing)
    async_register_intents(hass, skill_router)
    entry.async_on_unload(lambda: async_remove_intents(hass))
    try:
        await async_install_sentences(hass)
    except OSError as err:
        _LOGGER.warning(
            "Could not install the Assist sentences (%s)", type(err).__name__
        )
    else:
        await async_reload_assist(hass)
    entry.async_on_unload(lambda: async_remove_sentences(hass))

    transport.async_start()
    entry.async_on_unload(transport.async_stop)
    _async_schedule_briefing(hass, entry, runtime)
    _async_register_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HearthkeeperConfigEntry
) -> bool:
    """Unload a config entry."""
    return True


def _resolve_pipeline(
    hass: HomeAssistant, raw_pipeline_id: object
) -> PipelineSettings | None:
    """Resolve the selected Assist pipeline."""
    pipeline_id = _str_or_none(raw_pipeline_id)
    if pipeline_id is None:
        return None

    from homeassistant.components.assist_pipeline import PipelineNotFound

    try:
        return async_get_pipeline_settings(hass, pipeline_id)
    except KeyError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="pipeline_not_ready"
        ) from err
    except PipelineNotFound as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="pipeline_not_found",
            translation_placeholders={"pipeline_id": pipeline_id},
        ) from err


def _allowed_chats(telegram_entry: ConfigEntry) -> set[int]:
    """Read the chat allowlist from the core telegram_bot subentries."""
    chats: set[int] = set()
    for subentry in telegram_entry.subentries.values():
        if subentry.subentry_type != TELEGRAM_SUBENTRY_ALLOWED_CHATS:
            continue
        chat_id = subentry.data.get("chat_id")
        if isinstance(chat_id, int):
            chats.add(chat_id)
    return chats


def _str_or_none(value: object) -> str | None:
    """Return the value when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _str_list(value: object) -> tuple[str, ...]:
    """Return the entity ids of a multi-select option."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


@callback
def _async_schedule_briefing(
    hass: HomeAssistant,
    entry: HearthkeeperConfigEntry,
    runtime: HearthkeeperRuntime,
) -> None:
    """Send the briefing every day at the configured time."""
    if not runtime.brief_enabled:
        return

    hour, minute = parse_brief_time(runtime.brief_time)

    @callback
    def _time_changed(now: object) -> None:
        hass.async_create_task(
            runtime.brain.async_send_briefing(),
            "hearthkeeper morning briefing",
        )

    entry.async_on_unload(
        async_track_time_change(hass, _time_changed, hour=hour, minute=minute, second=0)
    )


def _as_int(value: object) -> int | None:
    """Coerce a service argument to an integer chat id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _loaded_runtimes(hass: HomeAssistant) -> list[HearthkeeperRuntime]:
    """Return runtimes of all loaded Hearthkeeper entries."""
    return [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register component services once."""
    handlers: dict[str, Callable[[ServiceCall], Awaitable[None]]] = {
        SERVICE_SAY: _async_handle_say,
        SERVICE_RESET: _async_handle_reset,
        SERVICE_BRIEF: _async_handle_brief,
    }
    for service, handler in handlers.items():
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler)


async def _async_handle_say(call: ServiceCall) -> None:
    """Send a text or voice message to a chat."""
    chat_id = _as_int(call.data.get(ATTR_CHAT_ID))
    text = call.data.get(ATTR_TEXT)
    mode = call.data.get(ATTR_MODE)
    if chat_id is None or not isinstance(text, str) or not text.strip():
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_service_data"
        )
    if mode is not None and (
        not isinstance(mode, str)
        or mode not in (REPLY_MODE_TEXT, REPLY_MODE_VOICE, REPLY_MODE_BOTH)
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_service_data"
        )

    for runtime in _loaded_runtimes(call.hass):
        if runtime.handles_chat(chat_id):
            await runtime.brain.async_say(chat_id, text, mode)
            return

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_chat",
        translation_placeholders={"chat_id": str(chat_id)},
    )


async def _async_handle_reset(call: ServiceCall) -> None:
    """Forget the conversation context of one or every chat."""
    raw_chat_id = call.data.get(ATTR_CHAT_ID)
    chat_id = _as_int(raw_chat_id) if raw_chat_id is not None else None
    if raw_chat_id is not None and chat_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_service_data"
        )

    runtimes = _loaded_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entries"
        )

    for runtime in runtimes:
        if chat_id is None or runtime.handles_chat(chat_id):
            await runtime.chats.async_reset(chat_id)


async def _async_handle_brief(call: ServiceCall) -> None:
    """Send the briefing now, to one chat or to every known private chat."""
    raw_chat_id = call.data.get(ATTR_CHAT_ID)
    chat_id = _as_int(raw_chat_id) if raw_chat_id is not None else None
    if raw_chat_id is not None and chat_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_service_data"
        )

    runtimes = _loaded_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entries"
        )

    targets = runtimes
    if chat_id is not None:
        targets = [runtime for runtime in runtimes if runtime.handles_chat(chat_id)]
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_chat",
                translation_placeholders={"chat_id": str(chat_id)},
            )

    for runtime in targets:
        await runtime.brain.async_send_briefing(chat_id)
