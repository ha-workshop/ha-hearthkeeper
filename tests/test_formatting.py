"""Tests for the Markdown to Telegram HTML conversion."""

from __future__ import annotations

from custom_components.hearthkeeper.formatting import to_telegram_html


def test_agent_markdown_becomes_telegram_html() -> None:
    """The reply from the screenshot renders with bold text and bullets."""
    text = (
        "Я — ваш голосовой помощник для управления умным домом через Home Assistant. "
        "Я умею:\n\n"
        "* **Управлять устройствами:** включать и выключать свет, розетки.\n"
        "* **Контролировать медиаплееры:** ставить музыку на паузу.\n"
        "* **Работать с пылесосом:** отправлять его на уборку.\n\n"
        "Просто скажите, что нужно сделать, и я помогу!"
    )

    result = to_telegram_html(text)

    assert "<b>Управлять устройствами:</b>" in result
    assert "<b>Контролировать медиаплееры:</b>" in result
    assert "• <b>Управлять устройствами" in result
    assert "**" not in result
    assert "<ul>" not in result


def test_inline_code_bold_italic_strike_and_links() -> None:
    """The common inline markers all map to supported tags."""
    text = (
        "Смотри `light.kitchen` и __важно__, это *курсив* и ~~старое~~.\n"
        "Ссылка: [документация](https://example.com/a?b=1&c=2)."
    )

    result = to_telegram_html(text)

    assert "<code>light.kitchen</code>" in result
    assert "<b>важно</b>" in result
    assert "<i>курсив</i>" in result
    assert "<s>старое</s>" in result
    assert '<a href="https://example.com/a?b=1&amp;c=2">документация</a>' in result


def test_headings_and_rules() -> None:
    """Headings lose their hashes and rules become a divider."""
    result = to_telegram_html("# Сводка\n\n---\n\n### Дом")

    assert "<b>Сводка</b>" in result
    assert "<b>Дом</b>" in result
    assert "#" not in result
    assert "──────" in result


def test_plain_text_is_escaped() -> None:
    """Anything that looks like HTML but is not ours stays text."""
    result = to_telegram_html("Температура < 20 & влажность > 60")

    assert "&lt; 20 &amp; влажность &gt; 60" in result


def test_entity_ids_keep_their_underscores() -> None:
    """Underscores inside a word are not italics."""
    result = to_telegram_html("Выключи light.kitchen_ceiling и climate.living_room")

    assert "light.kitchen_ceiling" in result
    assert "climate.living_room" in result
    assert "<i>" not in result


def test_unpaired_markers_are_left_alone() -> None:
    """A malformed answer must not produce broken HTML."""
    result = to_telegram_html("Цена **не закрыта и *звёздочка")

    assert "**не закрыта" in result
    assert "<b>" not in result


def test_code_blocks_are_not_formatted_inside() -> None:
    """Bold markers inside a fence stay literal."""
    result = to_telegram_html("```yaml\nlight: **не жирный**\n```")

    assert "<pre>light: **не жирный**</pre>" in result
    assert "<b>" not in result


def test_empty_text() -> None:
    """Nothing to format, nothing to send."""
    assert to_telegram_html("") == ""
