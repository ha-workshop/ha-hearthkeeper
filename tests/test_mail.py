"""Tests for the mail digest and the in-memory mailbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.hearthkeeper.brief import BriefingSources
from custom_components.hearthkeeper.mail import (
    Mailbox,
    _summary_text,
    async_mail_digest,
)


def _fire(hass: HomeAssistant, **data: Any) -> None:
    """Fire a message event like the IMAP integration does."""
    hass.bus.async_fire(
        "imap_content",
        {
            "entry_id": "imap-entry",
            "uid": "1",
            "sender": "Alice <alice@example.com>",
            "subject": "Dinner on Friday",
            "date": datetime(2026, 11, 19, 9, 30, tzinfo=dt_util.UTC),
        }
        | data,
    )


async def test_mail_digest_without_sources(hass: HomeAssistant) -> None:
    """No sensors means no mail section."""
    assert await async_mail_digest(hass, BriefingSources(), "ru") is None


async def test_mail_digest_reads_counters(hass: HomeAssistant) -> None:
    """Counters are shown without any AI involvement."""
    hass.states.async_set(
        "sensor.mail", "3", {"friendly_name": "IMAP (me@example.com)"}
    )

    text = await async_mail_digest(
        hass, BriefingSources(mail=("sensor.mail",)), "ru"
    )

    assert text == "✉️ Почта\n• IMAP (me@example.com): 3"


async def test_mail_digest_empty_and_missing(hass: HomeAssistant) -> None:
    """Zero unread and missing sensors are told apart."""
    hass.states.async_set("sensor.mail", "0", {"friendly_name": "Почта"})

    empty = await async_mail_digest(
        hass, BriefingSources(mail=("sensor.mail",)), "ru"
    )
    missing = await async_mail_digest(
        hass, BriefingSources(mail=("sensor.gone",)), "ru"
    )

    assert empty == "✉️ Почта: новых писем нет"
    assert missing == "✉️ Почта: недоступна"


async def test_mailbox_keeps_recent_messages(hass: HomeAssistant) -> None:
    """Events are reduced to sender, subject and date, newest first."""
    mailbox = Mailbox(hass)
    mailbox.async_start()
    _fire(hass)
    _fire(hass, uid="2", sender="Bob", subject="Invoice")
    _fire(hass, uid="2", sender="Bob", subject="Invoice")  # duplicate
    _fire(hass, sender=42, subject="Broken")  # ignored

    messages = mailbox.recent(hass, ("sensor.mail",), limit=5)
    mailbox.async_stop()

    assert [(message.sender, message.subject) for message in messages] == [
        ("Bob", "Invoice"),
        ("Alice <alice@example.com>", "Dinner on Friday"),
    ]
    assert mailbox.recent(hass, ("sensor.mail",)) == ()


async def test_mail_digest_lists_messages_locally(hass: HomeAssistant) -> None:
    """Without the cloud opt-in, senders and subjects stay local."""
    hass.states.async_set("sensor.mail", "2", {"friendly_name": "Почта"})
    mailbox = Mailbox(hass)
    mailbox.async_start()
    _fire(hass)

    text = await async_mail_digest(
        hass,
        BriefingSources(mail=("sensor.mail",)),
        "ru",
        mailbox=mailbox,
    )
    mailbox.async_stop()

    assert text is not None
    assert "Почта: 2" in text
    assert "Alice <alice@example.com> — Dinner on Friday" in text


async def test_mail_digest_uses_ai_when_allowed(hass: HomeAssistant) -> None:
    """With the opt-in the preferred AI Task writes the summary."""
    hass.states.async_set("sensor.mail", "1", {"friendly_name": "Почта"})
    mailbox = Mailbox(hass)
    mailbox.async_start()
    _fire(hass)
    calls: list[ServiceCall] = []

    async def _generate(call: ServiceCall) -> dict[str, Any]:
        calls.append(call)
        return {"conversation_id": "c1", "data": {"summary": "• Alice: dinner"}}

    hass.services.async_register(
        "ai_task",
        "generate_data",
        _generate,
        supports_response=SupportsResponse.ONLY,
    )

    text = await async_mail_digest(
        hass,
        BriefingSources(mail=("sensor.mail",)),
        "en",
        mailbox=mailbox,
        allow_cloud=True,
    )
    mailbox.async_stop()

    assert text == "✉️ Mail (AI summary)\n• Alice: dinner"
    assert len(calls) == 1
    instructions = calls[0].data["instructions"]
    assert "Alice <alice@example.com>" in instructions
    assert "Dinner on Friday" in instructions


async def test_mail_digest_does_not_call_ai_without_opt_in(
    hass: HomeAssistant,
) -> None:
    """The AI Task is never touched while the option is off."""
    hass.states.async_set("sensor.mail", "1", {"friendly_name": "Почта"})
    mailbox = Mailbox(hass)
    mailbox.async_start()
    _fire(hass)
    called = False

    async def _generate(call: ServiceCall) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"data": "nope"}

    hass.services.async_register(
        "ai_task",
        "generate_data",
        _generate,
        supports_response=SupportsResponse.ONLY,
    )

    text = await async_mail_digest(
        hass,
        BriefingSources(mail=("sensor.mail",)),
        "en",
        mailbox=mailbox,
        allow_cloud=False,
    )
    mailbox.async_stop()

    assert called is False
    assert text is not None
    assert text.startswith("✉️ Mail\n")


async def test_mail_digest_falls_back_when_ai_fails(hass: HomeAssistant) -> None:
    """A broken AI Task leaves the local list in place."""
    hass.states.async_set("sensor.mail", "1", {"friendly_name": "Почта"})
    mailbox = Mailbox(hass)
    mailbox.async_start()
    _fire(hass)

    async def _generate(call: ServiceCall) -> dict[str, Any]:
        raise HomeAssistantError("provider is down")

    hass.services.async_register(
        "ai_task",
        "generate_data",
        _generate,
        supports_response=SupportsResponse.ONLY,
    )

    text = await async_mail_digest(
        hass,
        BriefingSources(mail=("sensor.mail",)),
        "ru",
        mailbox=mailbox,
        allow_cloud=True,
    )
    mailbox.async_stop()

    assert text is not None
    assert text.startswith("AI-сводка недоступна")
    assert "Dinner on Friday" in text


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("  hello  ", "hello"),
        ({"summary": " bullets "}, "bullets"),
        ({"result": "ok"}, "ok"),
        ({"unknown": "x"}, None),
        (None, None),
        (42, None),
    ],
)
def test_summary_text(data: object, expected: str | None) -> None:
    """AI Tasks return either a string or a small dict."""
    assert _summary_text(data) == expected
