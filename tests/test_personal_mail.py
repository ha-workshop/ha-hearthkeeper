"""Tests for the mailbox each user connects in the Mini App."""

from __future__ import annotations

import imaplib
from typing import Any, ClassVar

import pytest
from homeassistant.core import HomeAssistant

from custom_components.hearthkeeper import personal_mail as pm
from custom_components.hearthkeeper.mail import async_personal_mail_digest
from custom_components.hearthkeeper.personal_mail import (
    MailboxError,
    PersonalMail,
    PersonalMailbox,
    validate,
)

HEADER = (
    b"From: =?utf-8?b?0JzQsNGI0LA=?= <masha@example.com>\r\n"
    b"Subject: =?utf-8?b?0KHRh9GR0YIg0LfQsCDRgdCy0LXRgg==?=\r\n"
    b"Date: Fri, 20 Nov 2026 09:15:00 +0200\r\n\r\n"
)


class FakeImap:
    """Answer like an IMAP server with three unread messages."""

    instances: ClassVar[list[FakeImap]] = []
    password = "app-pass"

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        if host == "down.example.com":
            raise OSError("refused")
        self.host, self.port = host, port
        self.commands: list[str] = []
        FakeImap.instances.append(self)

    def login(self, user: str, password: str) -> None:
        if password != self.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly, "the mailbox must be opened read-only"
        return ("OK", [b"3"]) if folder == '"INBOX"' else ("NO", [b""])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.commands.append(f"{command} {' '.join(str(a) for a in args)}")
        if command == "search":
            return "OK", [b"1 2 3"]
        assert "BODY.PEEK" in args[-1], "headers must be read without marking"
        return "OK", [(b"1 (BODY[HEADER] {120}", HEADER), b")"]

    def logout(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeImap.instances.clear()
    monkeypatch.setattr(pm.imaplib, "IMAP4_SSL", FakeImap)


def test_validate() -> None:
    """Hosts are checked; an empty password keeps the saved one."""
    box = validate(
        {"host": " IMAP.Yandex.ru ", "username": " oleg@yandex.ru ", "password": "x"},
        None,
    )
    assert (box.host, box.username, box.port, box.folder) == (
        "imap.yandex.ru",
        "oleg@yandex.ru",
        993,
        "INBOX",
    )
    kept = validate({"host": "imap.yandex.ru", "username": "oleg@yandex.ru"}, box)
    assert kept.password == "x"

    for raw, code in (
        ({"host": "localhost", "username": "a"}, "bad_host"),
        ({"host": "imap.x.ru/../", "username": "a", "password": "p"}, "bad_host"),
        ({"host": "imap.x.ru", "username": "", "password": "p"}, "bad_value"),
        (
            {"host": "imap.x.ru", "username": "a", "password": "p", "port": 0},
            "bad_value",
        ),
        (
            {"host": "imap.x.ru", "username": "a", "password": "p", "folder": 'a"b'},
            "bad_value",
        ),
        ({"host": "imap.other.ru", "username": "oleg@yandex.ru"}, "need_password"),
    ):
        with pytest.raises(MailboxError) as err:
            validate(raw, box)
        assert err.value.code == code


async def test_save_checks_login_and_keeps_password_private(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Only a working login is saved, and the page never sees the password."""
    personal = PersonalMail(hass)
    await personal.async_load()
    bad = PersonalMailbox(host="imap.x.ru", username="a", password="nope")
    with pytest.raises(MailboxError) as err:
        await personal.async_set(42, bad)
    assert err.value.code == "auth"
    assert personal.get(42) is None

    good = PersonalMailbox(host="imap.x.ru", username="a", password="app-pass")
    unread = await personal.async_set(42, good)
    assert unread.count == 3
    assert unread.headers[0][:2] == ("Маша", "Счёт за свет")
    assert "password" not in good.public()

    restored = PersonalMail(hass)
    await restored.async_load()
    assert restored.get(42) == good

    await restored.async_remove(42)
    assert restored.get(42) is None


async def test_unreachable_server(hass: HomeAssistant) -> None:
    """A server that refuses the connection gives a clear code."""
    personal = PersonalMail(hass)
    with pytest.raises(MailboxError) as err:
        await personal.async_unread(
            PersonalMailbox(host="down.example.com", username="a", password="p")
        )
    assert err.value.code == "connect"


async def test_personal_digest(hass: HomeAssistant) -> None:
    """The digest lists the unread count and the newest senders."""
    personal = PersonalMail(hass)
    box = PersonalMailbox(host="imap.x.ru", username="a", password="app-pass")

    text = await async_personal_mail_digest(personal, box, "ru")

    assert text.splitlines()[0] == "✉️ Почта: 3"
    assert "• Маша — Счёт за свет" in text

    box.password = "changed"
    assert await async_personal_mail_digest(personal, box, "ru") == (
        "✉️ Почта: недоступна"
    )
