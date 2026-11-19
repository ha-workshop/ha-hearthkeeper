"""Built-in skills for calendar, home status and the daily briefing.

The skills are reachable in two ways:

* from Telegram, by matching a short localized phrase before the text reaches
  the conversation agent, so they also work with local agents that know
  nothing about Home Assistant;
* from Assist, as regular intents registered with ``homeassistant.helpers.
  intent``. Their sentences are installed into ``custom_sentences`` so the
  default agent can recognize the same phrases.

Content of calendars is never logged above ``DEBUG``.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent as intent_helper

from .brief import (
    BriefingSources,
    async_build_briefing,
    async_calendar_day,
    home_status_text,
)
from .const import DEFAULT_LANGUAGE

_LOGGER = logging.getLogger(__name__)

INTENT_BRIEF: Final = "HearthkeeperBrief"
INTENT_CALENDAR: Final = "HearthkeeperCalendar"
INTENT_HOME: Final = "HearthkeeperHome"
INTENT_TYPES: Final = (INTENT_BRIEF, INTENT_CALENDAR, INTENT_HOME)

SENTENCES_DIR: Final = "custom_sentences"
SENTENCES_FILE: Final = "hearthkeeper.yaml"
_SENTENCES_MARKER: Final = "# Managed by Hearthkeeper, overwritten on start."

DAY_TODAY: Final = "today"
DAY_TOMORROW: Final = "tomorrow"

_SKILL_TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "calendar_missing": "Календари не настроены в Hearthkeeper.",
        "home_disabled": "Сводка по дому отключена в настройках Hearthkeeper.",
    },
    "en": {
        "calendar_missing": "No calendars are configured in Hearthkeeper.",
        "home_disabled": "The home summary is disabled in the Hearthkeeper options.",
    },
}

_PATTERNS: dict[str, tuple[tuple[str, str, re.Pattern[str]], ...]] = {
    "ru": (
        (
            INTENT_BRIEF,
            "",
            re.compile(
                r"(?:сводка|брифинг|дайджест)(?: дня| на день| на сегодня)?"
            ),
        ),
        (
            INTENT_CALENDAR,
            DAY_TODAY,
            re.compile(
                r"(?:"
                r"что(?: у меня)?(?: сегодня| на сегодня)"
                r"(?: (?:планы|дела|события|встречи))?"
                r"|какие(?: у меня)?(?: сегодня| на сегодня)?"
                r" (?:планы|дела|события|встречи)"
                r"|какие(?: у меня)? (?:планы|дела|события|встречи)(?: на)? сегодня"
                r"|(?:мои )?планы(?: на)? сегодня"
                r"|расписание(?: на)? сегодня"
                r")"
            ),
        ),
        (
            INTENT_CALENDAR,
            DAY_TOMORROW,
            re.compile(
                r"(?:"
                r"что(?: у меня)?(?: завтра| на завтра)"
                r"(?: (?:планы|дела|события|встречи))?"
                r"|какие(?: у меня)?(?: завтра| на завтра)?"
                r" (?:планы|дела|события|встречи)"
                r"|какие(?: у меня)? (?:планы|дела|события|встречи)(?: на)? завтра"
                r"|(?:мои )?планы(?: на)? завтра"
                r"|расписание(?: на)? завтра"
                r")"
            ),
        ),
        (
            INTENT_HOME,
            "",
            re.compile(
                r"(?:"
                r"(?:что|как)(?: сейчас| у нас)? дома"
                r"|статус дома"
                r"|что (?:включено|открыто)(?: дома)?"
                r"|что происходит дома"
                r")"
            ),
        ),
    ),
    "en": (
        (
            INTENT_BRIEF,
            "",
            re.compile(
                r"(?:briefing|summary|daily (?:briefing|summary))"
                r"(?: for today)?"
            ),
        ),
        (
            INTENT_CALENDAR,
            DAY_TODAY,
            re.compile(
                r"(?:"
                r"what(?:s| is)? (?:on |happening )?(?:today|for today)"
                r"|what do i have today"
                r"|todays? (?:schedule|agenda|plans)"
                r"|(?:my )?plans for today"
                r"|schedule for today"
                r")"
            ),
        ),
        (
            INTENT_CALENDAR,
            DAY_TOMORROW,
            re.compile(
                r"(?:"
                r"what(?:s| is)? (?:on |happening )?(?:tomorrow|for tomorrow)"
                r"|what do i have tomorrow"
                r"|tomorrows? (?:schedule|agenda|plans)"
                r"|(?:my )?plans for tomorrow"
                r"|schedule for tomorrow"
                r")"
            ),
        ),
        (
            INTENT_HOME,
            "",
            re.compile(
                r"(?:"
                r"home status"
                r"|status of (?:the )?home"
                r"|what(?:s| is)? (?:on|open|happening)(?: at)? home"
                r"|anything (?:on|open) at home"
                r")"
            ),
        ),
    ),
}

SENTENCES: dict[str, dict[str, list[dict[str, Any]]]] = {
    "ru": {
        INTENT_BRIEF: [
            {"sentences": ["сводка", "брифинг", "сводка дня", "дайджест"]}
        ],
        INTENT_CALENDAR: [
            {
                "sentences": [
                    "что у меня сегодня",
                    "что сегодня",
                    "какие у меня сегодня планы",
                    "какие планы на сегодня",
                    "планы на сегодня",
                    "расписание на сегодня",
                ],
                "slots": {"day": DAY_TODAY},
            },
            {
                "sentences": [
                    "что у меня завтра",
                    "что завтра",
                    "какие у меня завтра планы",
                    "какие планы на завтра",
                    "планы на завтра",
                    "расписание на завтра",
                ],
                "slots": {"day": DAY_TOMORROW},
            },
        ],
        INTENT_HOME: [
            {
                "sentences": [
                    "что дома",
                    "как дома",
                    "статус дома",
                    "что включено дома",
                    "что открыто дома",
                    "что происходит дома",
                ]
            }
        ],
    },
    "en": {
        INTENT_BRIEF: [
            {"sentences": ["briefing", "summary", "daily briefing", "daily summary"]}
        ],
        INTENT_CALENDAR: [
            {
                "sentences": [
                    "what is on today",
                    "what is happening today",
                    "what do i have today",
                    "today schedule",
                    "today plans",
                ],
                "slots": {"day": DAY_TODAY},
            },
            {
                "sentences": [
                    "what is on tomorrow",
                    "what is happening tomorrow",
                    "what do i have tomorrow",
                    "tomorrow schedule",
                    "tomorrow plans",
                ],
                "slots": {"day": DAY_TOMORROW},
            },
        ],
        INTENT_HOME: [
            {
                "sentences": [
                    "home status",
                    "status of the home",
                    "what is on at home",
                    "what is open at home",
                    "anything on at home",
                ]
            }
        ],
    },
}


@dataclass(frozen=True, slots=True)
class SkillRequest:
    """One matched skill call."""

    intent: str
    day: str = ""


def normalize(text: str) -> str:
    """Lowercase a phrase and remove punctuation used in chat messages."""
    cleaned = text.strip().lower().replace("ё", "е")
    cleaned = cleaned.replace("'", "").replace("’", "")
    cleaned = re.sub(r"[.!?…,;:]+$", "", cleaned)
    return re.sub(r"\s+", " ", cleaned)


class SkillRouter:
    """Match short phrases and run the matching skill."""

    def __init__(self, hass: HomeAssistant, sources: BriefingSources) -> None:
        self.hass = hass
        self.sources = sources

    @staticmethod
    def match(text: str, language: str) -> SkillRequest | None:
        """Return the skill for a phrase, or None when the agent should answer."""
        normalized = normalize(text)
        if not normalized:
            return None

        patterns = _PATTERNS.get(language, _PATTERNS[DEFAULT_LANGUAGE])
        for intent_type, day, pattern in patterns:
            if pattern.fullmatch(normalized):
                return SkillRequest(intent_type, day)
        return None

    async def async_run(self, request: SkillRequest, language: str) -> str:
        """Run a matched skill and return its text."""
        texts = _SKILL_TEXTS.get(language, _SKILL_TEXTS[DEFAULT_LANGUAGE])

        if request.intent == INTENT_BRIEF:
            return await async_build_briefing(self.hass, self.sources, language)

        if request.intent == INTENT_CALENDAR:
            answer = await async_calendar_day(
                self.hass,
                self.sources,
                language,
                tomorrow=request.day == DAY_TOMORROW,
            )
            return answer or texts["calendar_missing"]

        if request.intent == INTENT_HOME:
            answer = home_status_text(self.hass, self.sources, language)
            return answer or texts["home_disabled"]

        raise ValueError(f"Unknown skill: {request.intent}")


class _SkillIntent(intent_helper.IntentHandler):
    """Expose one skill to the Assist intent engine."""

    def __init__(self, router: SkillRouter, intent_type: str) -> None:
        self.intent_type = intent_type
        self._router = router

    async def async_handle(self, intent_obj: intent_helper.Intent) -> Any:
        """Run the skill and turn its text into an intent response."""
        day = ""
        if self.intent_type == INTENT_CALENDAR:
            raw = intent_obj.slots.get("day", {}).get("value", DAY_TODAY)
            day = DAY_TOMORROW if raw == DAY_TOMORROW else DAY_TODAY

        response = intent_helper.IntentResponse(language=intent_obj.language)
        response.async_set_speech(
            await self._router.async_run(
                SkillRequest(self.intent_type, day), intent_obj.language
            )
        )
        return response


@callback
def async_register_intents(hass: HomeAssistant, router: SkillRouter) -> None:
    """Register the skills as Assist intents."""
    for intent_type in INTENT_TYPES:
        intent_helper.async_register(hass, _SkillIntent(router, intent_type))


@callback
def async_remove_intents(hass: HomeAssistant) -> None:
    """Remove only our intents, leaving other integrations alone."""
    for intent_type in INTENT_TYPES:
        intent_helper.async_remove(hass, intent_type)


def _render_sentences(language: str) -> str:
    """Render the sentence file for one language."""
    payload = {"language": language, "intents": SENTENCES[language]}
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    return f"{_SENTENCES_MARKER}\n{body}"


def _write_sentences(path: Path, content: str) -> None:
    """Write a sentence file when its content changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if not existing.startswith(_SENTENCES_MARKER):
            _LOGGER.warning(
                "Not overwriting %s: it was not created by Hearthkeeper", path
            )
            return
        if existing == content:
            return
    path.write_text(content, encoding="utf-8")
    _LOGGER.debug("Installed Hearthkeeper sentences at %s", path)


def _remove_sentences(path: Path) -> None:
    """Remove only files written by Hearthkeeper."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    if not content.startswith(_SENTENCES_MARKER):
        return
    path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        path.parent.rmdir()


async def async_install_sentences(hass: HomeAssistant) -> None:
    """Install the intent sentences the default Assist agent reads."""
    for language in SENTENCES:
        path = Path(hass.config.path(SENTENCES_DIR, language, SENTENCES_FILE))
        await hass.async_add_executor_job(
            _write_sentences, path, _render_sentences(language)
        )


async def async_reload_assist(hass: HomeAssistant) -> None:
    """Make the default agent pick up the freshly installed sentences."""
    if "conversation" not in hass.config.components:
        return
    try:
        await hass.services.async_call("conversation", "reload", blocking=True)
    except HomeAssistantError as err:
        _LOGGER.debug("Could not reload Assist intents (%s)", type(err).__name__)


async def async_remove_sentences(hass: HomeAssistant) -> None:
    """Remove the sentence files on unload when they are still ours."""
    for language in SENTENCES:
        path = Path(hass.config.path(SENTENCES_DIR, language, SENTENCES_FILE))
        await hass.async_add_executor_job(_remove_sentences, path)
