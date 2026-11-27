"""Constants for the Hearthkeeper integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hearthkeeper"
NAME: Final = "Hearthkeeper"

TELEGRAM_DOMAIN: Final = "telegram_bot"
TELEGRAM_SUBENTRY_ALLOWED_CHATS: Final = "allowed_chat_ids"

# Config entry data
CONF_TELEGRAM_ENTRY: Final = "telegram_entry"

# Options. Voice settings (agent, STT, TTS, voice, language) are not duplicated
# here: they live in the Assist pipeline the user picks.
CONF_PIPELINE_ID: Final = "pipeline_id"
CONF_REPLY_MODE: Final = "reply_mode"
CONF_ALLOW_GROUPS: Final = "allow_groups"
CONF_TODO_ENTITY: Final = "todo_entity"
CONF_KEEP_AUDIO: Final = "keep_audio"
CONF_ALLOW_CLOUD_FOR_MAIL: Final = "allow_cloud_for_mail"
CONF_MAX_VOICE_SECONDS: Final = "max_voice_seconds"

# Mini App. Telegram opens Mini Apps over HTTPS only, so an explicit public
# address can be set when the automatic external/cloud URL is not the one the
# Telegram clients can reach (for example a tunnel in front of Home Assistant).
CONF_WEBAPP_URL: Final = "webapp_url"

# Connection plugins: GitHub repositories (``owner/repo`` or
# ``owner/repo@ref``) with a ``connection.json`` at the root, added the way
# HACS adds custom repositories.
CONF_CONNECTION_REPOS: Final = "connection_repositories"

# Briefing. Sources are entity ids picked in the options flow, so the same
# calendars, weather entity and mail sensors the user already sees in Home
# Assistant feed the Telegram summary.
CONF_BRIEF_ENABLED: Final = "brief_enabled"
CONF_BRIEF_TIME: Final = "brief_time"
CONF_BRIEF_CALENDARS: Final = "brief_calendars"
CONF_BRIEF_WEATHER: Final = "brief_weather"
CONF_BRIEF_MAIL: Final = "brief_mail"
CONF_BRIEF_HOME: Final = "brief_home"
CONF_BRIEF_CHATS: Final = "brief_chats"
# Telegram users who may use the bot and the Mini App; empty means everyone
# the core Telegram integration already lets through.
CONF_USERS: Final = "users"
# Telegram users who may change the shared sources from the Mini App.
CONF_ADMINS: Final = "admins"
CONF_QUIET_START: Final = "quiet_start"
CONF_QUIET_END: Final = "quiet_end"

# Selector value that means "no pipeline, echo messages back".
PIPELINE_NONE: Final = "none"

REPLY_MODE_TEXT: Final = "text"
REPLY_MODE_VOICE: Final = "voice"
REPLY_MODE_BOTH: Final = "both"
REPLY_MODES: Final = (REPLY_MODE_TEXT, REPLY_MODE_VOICE, REPLY_MODE_BOTH)

# Scheduled briefings. Everyone may keep a few of them with their own time:
# a morning one with the plan for the day and an evening one with the results.
BRIEFING_MORNING: Final = "morning"
BRIEFING_EVENING: Final = "evening"
# A scheduled mail digest: the same text as /mail, at a time of the user's.
BRIEFING_MAIL: Final = "mail"
BRIEFING_KINDS: Final = (BRIEFING_MORNING, BRIEFING_EVENING, BRIEFING_MAIL)
MAX_BRIEFINGS: Final = 5

LANGUAGES: Final = ("ru", "en")

DEFAULT_LANGUAGE: Final = "ru"
DEFAULT_REPLY_MODE: Final = REPLY_MODE_TEXT
DEFAULT_ALLOW_GROUPS: Final = False
DEFAULT_MAX_VOICE_SECONDS: Final = 120
DEFAULT_ALLOW_CLOUD_FOR_MAIL: Final = False
DEFAULT_BRIEF_ENABLED: Final = False
DEFAULT_BRIEF_TIME: Final = "08:00"
# Every new private chat starts with its own morning briefing at this time;
# the user changes or turns it off in the Mini App.
NEW_USER_BRIEF_TIME: Final = "09:00"
DEFAULT_KEEP_AUDIO: Final = False
DEFAULT_BRIEF_HOME: Final = True
DEFAULT_QUIET_START: Final = "23:00"
DEFAULT_QUIET_END: Final = "07:00"

DEFAULT_OPTIONS: Final[dict[str, object]] = {
    CONF_PIPELINE_ID: None,
    CONF_REPLY_MODE: DEFAULT_REPLY_MODE,
    CONF_ALLOW_GROUPS: DEFAULT_ALLOW_GROUPS,
    CONF_TODO_ENTITY: None,
    CONF_KEEP_AUDIO: DEFAULT_KEEP_AUDIO,
    CONF_MAX_VOICE_SECONDS: DEFAULT_MAX_VOICE_SECONDS,
    CONF_ALLOW_CLOUD_FOR_MAIL: DEFAULT_ALLOW_CLOUD_FOR_MAIL,
    CONF_WEBAPP_URL: None,
    CONF_CONNECTION_REPOS: (),
    CONF_BRIEF_ENABLED: DEFAULT_BRIEF_ENABLED,
    CONF_BRIEF_TIME: DEFAULT_BRIEF_TIME,
    CONF_BRIEF_CALENDARS: (),
    CONF_BRIEF_WEATHER: None,
    CONF_BRIEF_MAIL: (),
    CONF_BRIEF_HOME: DEFAULT_BRIEF_HOME,
    CONF_BRIEF_CHATS: (),
    CONF_ADMINS: (),
    CONF_USERS: (),
    CONF_QUIET_START: DEFAULT_QUIET_START,
    CONF_QUIET_END: DEFAULT_QUIET_END,
}

# Services
SERVICE_SAY: Final = "say"
SERVICE_RESET: Final = "reset"
SERVICE_BRIEF: Final = "brief"
SERVICES: Final = (SERVICE_SAY, SERVICE_RESET, SERVICE_BRIEF)

ATTR_CHAT_ID: Final = "chat_id"
ATTR_TEXT: Final = "text"
ATTR_MODE: Final = "mode"
ATTR_KIND: Final = "kind"

# Storage
STORAGE_VERSION: Final = 1
STORAGE_CHATS: Final = f"{DOMAIN}.chats"
STORAGE_DIR: Final = f"{DOMAIN}/downloads"

# Bus events fired by the core telegram_bot integration
EVENT_TEXT: Final = "telegram_text"
EVENT_COMMAND: Final = "telegram_command"
EVENT_ATTACHMENT: Final = "telegram_attachment"
EVENT_CALLBACK: Final = "telegram_callback"
EVENT_SENT: Final = "telegram_sent"

# Limits
CONVERSATION_TIMEOUT: Final = 60
TELEGRAM_MESSAGE_LIMIT: Final = 4096
DEDUP_CACHE_SIZE: Final = 256
SENT_ID_CACHE_SIZE: Final = 50

# Audio. Telegram lets a bot download files up to 20 MB, and the pipeline gets
# the whole message at once, so both limits keep a long voice note from
# blocking the chat.
VOICE_TIMEOUT: Final = 120
TTS_TIMEOUT: Final = 60
MEDIA_TIMEOUT: Final = 60
VOICE_DOWNLOAD_LIMIT: Final = 20 * 1024 * 1024
VOICE_SAMPLE_RATE: Final = 16000
