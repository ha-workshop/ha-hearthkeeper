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
CONF_BRIEF_TIME: Final = "brief_time"
CONF_BRIEF_CHATS: Final = "brief_chats"
CONF_MAX_VOICE_SECONDS: Final = "max_voice_seconds"

# Selector value that means "no pipeline, echo messages back".
PIPELINE_NONE: Final = "none"

REPLY_MODE_TEXT: Final = "text"
REPLY_MODE_VOICE: Final = "voice"
REPLY_MODE_BOTH: Final = "both"
REPLY_MODES: Final = (REPLY_MODE_TEXT, REPLY_MODE_VOICE, REPLY_MODE_BOTH)

LANGUAGES: Final = ("ru", "en")

DEFAULT_LANGUAGE: Final = "ru"
DEFAULT_REPLY_MODE: Final = REPLY_MODE_TEXT
DEFAULT_ALLOW_GROUPS: Final = False
DEFAULT_MAX_VOICE_SECONDS: Final = 120

# M0 only exposes the settings that actually do something. Briefing and cloud
# options are added with the milestones that implement them.
DEFAULT_OPTIONS: Final[dict[str, object]] = {
    CONF_PIPELINE_ID: None,
    CONF_REPLY_MODE: DEFAULT_REPLY_MODE,
    CONF_ALLOW_GROUPS: DEFAULT_ALLOW_GROUPS,
    CONF_TODO_ENTITY: None,
    CONF_MAX_VOICE_SECONDS: DEFAULT_MAX_VOICE_SECONDS,
}

# Services
SERVICE_SAY: Final = "say"
SERVICE_RESET: Final = "reset"
SERVICE_BRIEF: Final = "brief"
SERVICES: Final = (SERVICE_SAY, SERVICE_RESET, SERVICE_BRIEF)

ATTR_CHAT_ID: Final = "chat_id"
ATTR_TEXT: Final = "text"
ATTR_MODE: Final = "mode"

# Storage
STORAGE_VERSION: Final = 1
STORAGE_CHATS: Final = f"{DOMAIN}.chats"

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
