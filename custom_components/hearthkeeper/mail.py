"""Mail digest built from IMAP events with an optional AI summary.

The IMAP integration only exposes unread counters and a ``imap_content`` bus
event for new messages. Bodies are never stored here: only sender, subject and
date, in memory, and only for the latest few messages. Nothing is sent to an
AI Task unless ``allow_cloud_for_mail`` is enabled in the options.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .brief import BriefingSources
    from .personal_mail import PersonalMail, PersonalMailbox

_LOGGER = logging.getLogger(__name__)

IMAP_EVENT: Final = "imap_content"
IMAP_DOMAIN: Final = "imap"
MAX_MESSAGES: Final = 20
DEFAULT_LIMIT: Final = 5
MAX_SUMMARY_LINES: Final = 900
AI_TASK_TIMEOUT: Final = 45.0

_TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "header": "✉️ Почта",
        "ai_header": "✉️ Почта (сводка через AI)",
        "no_mail": "новых писем нет",
        "not_configured": "почта не настроена в Hearthkeeper.",
        "unavailable": "недоступна",
        "ai_failed": "AI-сводка недоступна, последние письма:",
        "more": "… и ещё {count}",
        "no_sender": "без отправителя",
        "no_subject": "(без темы)",
        "instructions": (
            "Summarize the unread mail below for a home assistant user. "
            "Each line has sender, subject and date. Write 3 to 5 short bullets, "
            "one message per bullet: who wrote, what about, and why it may matter. "
            "Never invent senders, subjects or facts that are not listed. "
            "Do not greet and do not add headings. Answer in Russian."
        ),
    },
    "en": {
        "header": "✉️ Mail",
        "ai_header": "✉️ Mail (AI summary)",
        "no_mail": "no new mail",
        "not_configured": "mail is not configured in Hearthkeeper.",
        "unavailable": "unavailable",
        "ai_failed": "The AI summary is unavailable, latest messages:",
        "more": "… and {count} more",
        "no_sender": "no sender",
        "no_subject": "(no subject)",
        "instructions": (
            "Summarize the unread mail below for a home assistant user. "
            "Each line has sender, subject and date. Write 3 to 5 short bullets, "
            "one message per bullet: who wrote, what about, and why it may matter. "
            "Never invent senders, subjects or facts that are not listed. "
            "Do not greet and do not add headings. Answer in English."
        ),
    },
}


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One recent message, reduced to what the digest needs."""

    sender: str
    subject: str
    received: str
    entry_id: str
    uid: str


class Mailbox:
    """Keep the latest IMAP messages in memory for digests."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._messages: dict[tuple[str, str], MailMessage] = {}
        self._unsub: Any = None

    @callback
    def async_start(self) -> None:
        """Listen to new mail events until the config entry unloads."""
        if self._unsub is None:
            self._unsub = self.hass.bus.async_listen(
                IMAP_EVENT, self._async_handle_event
            )

    @callback
    def async_stop(self) -> None:
        """Stop listening and forget the cached subjects and senders."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._messages.clear()

    @callback
    def _async_handle_event(self, event: Event) -> None:
        """Remember a message without ever logging its content."""
        data = event.data
        sender = data.get("sender")
        subject = data.get("subject")
        if not isinstance(sender, str) or not isinstance(subject, str):
            return

        entry_id = data.get("entry_id")
        uid = data.get("uid")
        message = MailMessage(
            sender=sender.strip(),
            subject=subject.strip(),
            received=_as_text(data.get("date")),
            entry_id=entry_id if isinstance(entry_id, str) else "",
            uid=str(uid) if uid is not None else "",
        )
        key = (message.entry_id, message.uid or f"{message.sender}|{message.subject}")
        self._messages.pop(key, None)
        self._messages[key] = message
        while len(self._messages) > MAX_MESSAGES:
            self._messages.pop(next(iter(self._messages)))
        _LOGGER.debug("Mailbox now holds %s recent messages", len(self._messages))

    def recent(
        self, hass: HomeAssistant, sensors: tuple[str, ...], limit: int = DEFAULT_LIMIT
    ) -> tuple[MailMessage, ...]:
        """Return the newest messages that belong to the selected accounts."""
        entries = _mail_entry_ids(hass, sensors)
        messages = [
            message
            for message in self._messages.values()
            if not entries or not message.entry_id or message.entry_id in entries
        ]
        return tuple(reversed(messages[-limit:]))


def _as_text(value: object) -> str:
    """Turn an event date into an ISO string without raising."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return ""


@callback
def _mail_entry_ids(hass: HomeAssistant, sensors: tuple[str, ...]) -> set[str]:
    """Map the chosen IMAP sensors back to their config entries."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    entry_ids: set[str] = set()
    for entity_id in sensors:
        entity = entities.async_get(entity_id)
        if entity is None or entity.device_id is None:
            continue
        device = devices.async_get(entity.device_id)
        if device is None:
            continue
        entry_ids.update(
            identifier
            for domain, identifier in device.identifiers
            if domain == IMAP_DOMAIN
        )
    return entry_ids


def _format_date(value: str) -> str:
    """Format a stored date for the digest."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return ""
    return dt_util.as_local(parsed).strftime("%d.%m %H:%M")


def _sender(message: MailMessage, texts: dict[str, str]) -> str:
    return message.sender or texts["no_sender"]


def _subject(message: MailMessage, texts: dict[str, str]) -> str:
    subject = message.subject or texts["no_subject"]
    return subject if len(subject) <= 80 else f"{subject[:77]}…"


async def async_mail_digest(
    hass: HomeAssistant,
    sources: BriefingSources,
    language: str,
    *,
    mailbox: Mailbox | None = None,
    allow_cloud: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> str | None:
    """Build the mail section, or None when no sensors are configured."""
    if not sources.mail:
        return None

    texts = _TEXTS.get(language, _TEXTS["en"])
    counters = _counters(hass, sources)
    if not counters:
        return f"{texts['header']}: {texts['unavailable']}"

    messages = mailbox.recent(hass, sources.mail, limit) if mailbox else ()
    unread = sum(count for _, count in counters)
    if unread == 0 and not messages:
        return f"{texts['header']}: {texts['no_mail']}"

    if allow_cloud and messages:
        summary = await _async_ai_summary(hass, messages, texts)
        if summary:
            return f"{texts['ai_header']}\n{summary}"
        header = texts["ai_failed"]
    else:
        header = texts["header"]

    lines = [header]
    lines.extend(f"• {name}: {count}" for name, count in counters if count > 0)
    lines.extend(
        f"• {_sender(message, texts)} — {_subject(message, texts)}"
        for message in messages
    )
    return "\n".join(lines)


async def async_personal_mail_digest(
    personal: PersonalMail,
    box: PersonalMailbox,
    language: str,
    *,
    allow_cloud: bool = False,
) -> str:
    """Build the mail section from a user's own mailbox."""
    from .personal_mail import MailboxError

    texts = _TEXTS.get(language, _TEXTS["en"])
    try:
        unread = await personal.async_unread(box)
    except MailboxError as err:
        _LOGGER.debug("Personal mailbox is unavailable (%s)", err.code)
        return f"{texts['header']}: {texts['unavailable']}"
    if unread.count == 0:
        return f"{texts['header']}: {texts['no_mail']}"
    messages = tuple(
        MailMessage(
            sender=sender, subject=subject, received=received, entry_id="", uid=""
        )
        for sender, subject, received in unread.headers
    )
    if allow_cloud and messages:
        summary = await _async_ai_summary(personal.hass, messages, texts)
        if summary:
            return f"{texts['ai_header']}\n{summary}"
        header = texts["ai_failed"]
    else:
        header = texts["header"]
    lines = [f"{header}: {unread.count}"]
    lines.extend(
        f"• {_sender(message, texts)} — {_subject(message, texts)}"
        for message in messages
    )
    if unread.count > len(messages):
        lines.append(texts["more"].format(count=unread.count - len(messages)))
    return "\n".join(lines)


def _counters(
    hass: HomeAssistant, sources: BriefingSources
) -> list[tuple[str, int]]:
    """Read unread counters from the selected sensors."""
    counters: list[tuple[str, int]] = []
    for entity_id in sources.mail:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        count = _number(state.state)
        if count is None:
            continue
        name = state.attributes.get("friendly_name")
        counters.append((name if isinstance(name, str) and name else entity_id, count))
    return counters


def _number(value: object) -> int | None:
    """Round a numeric state to an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value)
    if isinstance(value, str):
        try:
            return round(float(value.replace(",", ".")))
        except ValueError:
            return None
    return None


async def _async_ai_summary(
    hass: HomeAssistant, messages: tuple[MailMessage, ...], texts: dict[str, str]
) -> str | None:
    """Ask the user's preferred AI Task to rank the recent messages."""
    payload = "\n".join(
        f"- {_sender(message, texts)} | {_subject(message, texts)} | "
        f"{_format_date(message.received)}"
        for message in messages
    )
    try:
        async with asyncio.timeout(AI_TASK_TIMEOUT):
            response = await hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "task_name": "Hearthkeeper mail digest",
                    "instructions": f"{texts['instructions']}\n\n{payload}",
                },
                blocking=True,
                return_response=True,
            )
    except TimeoutError:
        _LOGGER.debug("AI Task timed out while summarizing mail")
        return None
    except HomeAssistantError as err:
        _LOGGER.debug("AI Task failed for mail (%s)", type(err).__name__)
        return None

    if not isinstance(response, dict):
        return None
    summary = _summary_text(response.get("data"))
    if not summary:
        return None
    return summary[:MAX_SUMMARY_LINES]


def _summary_text(data: object) -> str | None:
    """Accept a string or a small dict from an AI Task."""
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, dict):
        for key in ("summary", "text", "response", "result", "data"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
