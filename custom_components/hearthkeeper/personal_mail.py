"""A mailbox each Telegram user connects for themselves in the Mini App.

Unlike the shared IMAP sensors of Home Assistant, a personal mailbox belongs
to one private chat. Hearthkeeper logs in over IMAP with TLS and reads only
the headers of unread messages (sender, subject, date) with ``BODY.PEEK``, so
nothing is marked as read and no body is fetched.

The password is kept in its own private store file, the way Home Assistant
keeps the credentials of its own integrations. It is never sent back to the
page, written to the log or included in diagnostics.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.header
import email.utils
import imaplib
import logging
import re
import ssl
from dataclasses import asdict, dataclass
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY: Final = f"{DOMAIN}.personal_mail"
STORAGE_VERSION: Final = 1
IMAP_TIMEOUT: Final = 15
MAX_HEADERS: Final = 5
_HOST: Final = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_FOLDER: Final = re.compile(r"^[^\x00-\x1f\"\\]{1,64}$")


class MailboxError(Exception):
    """Logging in or reading failed; ``code`` is safe to show to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class PersonalMailbox:
    """IMAP settings of one user."""

    host: str
    username: str
    password: str
    port: int = 993
    folder: str = "INBOX"

    def public(self) -> dict[str, Any]:
        """Return what the page may see: everything but the password."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "folder": self.folder,
        }


def validate(raw: object, previous: PersonalMailbox | None) -> PersonalMailbox:
    """Return settings from the page; an empty password keeps the saved one."""
    if not isinstance(raw, dict):
        raise MailboxError("bad_value")
    host = raw.get("host")
    username = raw.get("username")
    password = raw.get("password") or ""
    port = raw.get("port", 993)
    folder = raw.get("folder") or "INBOX"
    if not isinstance(host, str) or not _HOST.match(host.strip()):
        raise MailboxError("bad_host")
    if not isinstance(username, str) or not 0 < len(username.strip()) <= 254:
        raise MailboxError("bad_value")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise MailboxError("bad_value")
    if not isinstance(folder, str) or not _FOLDER.match(folder):
        raise MailboxError("bad_value")
    if not isinstance(password, str) or len(password) > 256:
        raise MailboxError("bad_value")
    host, username = host.strip().lower(), username.strip()
    if not password:
        if previous is None or previous.host != host or previous.username != username:
            raise MailboxError("need_password")
        password = previous.password
    return PersonalMailbox(
        host=host, username=username, password=password, port=port, folder=folder
    )


@dataclass(frozen=True, slots=True)
class Unread:
    """Unread count and the newest headers of a personal mailbox."""

    count: int
    headers: tuple[tuple[str, str, str], ...]  # sender, subject, ISO date


def _decode(value: str | None) -> str:
    """Decode an RFC 2047 header into plain text."""
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return " ".join("".join(parts).split())


def _read_unread(box: PersonalMailbox, context: ssl.SSLContext) -> Unread:
    """Log in, read unread headers and log out. Runs in the executor."""
    try:
        client = imaplib.IMAP4_SSL(
            box.host, box.port, ssl_context=context, timeout=IMAP_TIMEOUT
        )
    except (OSError, imaplib.IMAP4.error) as err:
        raise MailboxError("connect") from err
    try:
        try:
            client.login(box.username, box.password)
        except imaplib.IMAP4.error as err:
            raise MailboxError("auth") from err
        status, _ = client.select(f'"{box.folder}"', readonly=True)
        if status != "OK":
            raise MailboxError("folder")
        status, data = client.uid("search", None, "UNSEEN")
        if status != "OK":
            raise MailboxError("read")
        uids = data[0].split() if data and data[0] else []
        headers: list[tuple[str, str, str]] = []
        for uid in reversed(uids[-MAX_HEADERS:]):
            status, parts = client.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            raw = next(
                (part[1] for part in parts or () if isinstance(part, tuple)), None
            )
            if status != "OK" or not isinstance(raw, bytes):
                continue
            message = email.message_from_bytes(raw)
            name, address = email.utils.parseaddr(_decode(message.get("From")))
            try:
                date = email.utils.parsedate_to_datetime(message.get("Date", ""))
            except (TypeError, ValueError):
                date = None
            headers.append(
                (
                    name or address,
                    _decode(message.get("Subject")),
                    date.isoformat() if date else "",
                )
            )
        return Unread(count=len(uids), headers=tuple(headers))
    except (OSError, imaplib.IMAP4.error) as err:
        raise MailboxError("read") from err
    finally:
        with contextlib.suppress(OSError, imaplib.IMAP4.error):
            client.logout()


class PersonalMail:
    """Keep each chat's mailbox in a private store and read it on demand."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True
        )
        self._boxes: dict[int, PersonalMailbox] = {}

    async def async_load(self) -> None:
        """Read the saved mailboxes, dropping anything malformed."""
        data = await self._store.async_load() or {}
        for key, raw in data.items():
            try:
                chat_id = int(key)
                box = PersonalMailbox(**raw)
            except (TypeError, ValueError):
                continue
            self._boxes[chat_id] = box

    def get(self, chat_id: int) -> PersonalMailbox | None:
        """Return the mailbox of a chat."""
        return self._boxes.get(chat_id)

    async def async_unread(self, box: PersonalMailbox) -> Unread:
        """Return the unread headers; raise ``MailboxError`` on failure."""
        from homeassistant.util.ssl import client_context

        context = await self.hass.async_add_executor_job(client_context)
        try:
            async with asyncio.timeout(IMAP_TIMEOUT * 3):
                return await self.hass.async_add_executor_job(
                    _read_unread, box, context
                )
        except TimeoutError as err:
            raise MailboxError("connect") from err

    async def async_set(self, chat_id: int, box: PersonalMailbox) -> Unread:
        """Check the login, then save; the password is stored only on success."""
        unread = await self.async_unread(box)
        self._boxes[chat_id] = box
        await self._async_save()
        return unread

    async def async_remove(self, chat_id: int) -> None:
        """Forget a chat's mailbox and its password."""
        if self._boxes.pop(chat_id, None) is not None:
            await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {str(chat_id): asdict(box) for chat_id, box in self._boxes.items()}
        )
