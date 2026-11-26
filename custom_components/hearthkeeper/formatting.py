"""Turn the Markdown conversation agents write into Telegram HTML.

Telegram has no Markdown renderer of its own: a bot may send a small HTML
subset and must escape everything else. Conversation agents (including the
default Home Assistant agent) answer in Markdown, so ``**bold**`` and ``*``
bullets used to arrive as literal asterisks.

The converter is deliberately forgiving. It only rewrites markers that come in
pairs, leaves anything else as plain text and escapes HTML, so the result is
always valid for ``parse_mode="html"``.
"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BULLET = re.compile(r"^[ \t]*[-*+•][ \t]+", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_BOLD_UNDER = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])", re.DOTALL)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_ITALIC_STAR = re.compile(
    r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", re.DOTALL
)
_ITALIC_UNDER = re.compile(
    r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", re.DOTALL
)
_MULTI_BLANK = re.compile(r"\n{3,}")

# Placeholders keep code and links away from the inline rules. Private-use
# characters are not typed in chat messages, so they cannot collide.
_TOKEN = "\x00{}\x00"
_TOKEN_RE = re.compile(r"\x00([FLI]\d+)\x00")


def to_telegram_html(text: str) -> str:
    """Return ``text`` as the HTML subset Telegram renders."""
    if not text:
        return ""

    stored: dict[str, str] = {}
    prepared = _extract(text, stored)
    prepared = html.escape(prepared, quote=False)

    prepared = _HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", prepared)
    prepared = _RULE.sub("──────────", prepared)
    prepared = _BULLET.sub("• ", prepared)
    prepared = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", prepared)
    prepared = _BOLD_UNDER.sub(lambda m: f"<b>{m.group(1)}</b>", prepared)
    prepared = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", prepared)
    prepared = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", prepared)
    prepared = _ITALIC_UNDER.sub(lambda m: f"<i>{m.group(1)}</i>", prepared)

    prepared = _TOKEN_RE.sub(lambda m: stored[m.group(1)], prepared)
    # Markdown uses blank lines between blocks; Telegram shows them as gaps,
    # two are enough and keep long answers compact.
    return _MULTI_BLANK.sub("\n\n", prepared).strip()


def _extract(text: str, stored: dict[str, str]) -> str:
    """Replace code spans and links with tokens."""

    def keep_code(match: re.Match[str]) -> str:
        key = f"F{len(stored)}"
        code = html.escape(match.group(1).strip("\n"), quote=False)
        stored[key] = f"<pre>{code}</pre>"
        return _TOKEN.format(key)

    def keep_inline(match: re.Match[str]) -> str:
        key = f"L{len(stored)}"
        stored[key] = f"<code>{html.escape(match.group(1), quote=False)}</code>"
        return _TOKEN.format(key)

    def keep_link(match: re.Match[str]) -> str:
        key = f"I{len(stored)}"
        label = html.escape(match.group(1), quote=False)
        url = html.escape(match.group(2), quote=True)
        stored[key] = f'<a href="{url}">{label}</a>'
        return _TOKEN.format(key)

    text = _FENCE.sub(keep_code, text)
    text = _INLINE_CODE.sub(keep_inline, text)
    return _LINK.sub(keep_link, text)


__all__ = ["to_telegram_html"]
