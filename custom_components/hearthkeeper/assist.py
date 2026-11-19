"""Access to the Assist pipelines Hearthkeeper follows.

Hearthkeeper keeps no voice settings of its own. The pipeline edited in
Settings -> Voice assistants (``/config/voice-assistants/assistants``) already
holds the conversation agent, the STT engine, the TTS engine, the voice and the
language, so there is a single source of truth instead of two drifting copies.

The ``assist_pipeline`` integration is imported lazily: Hearthkeeper must stay
importable (and testable) when that integration or its dependencies are not
installed yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import DEFAULT_LANGUAGE

if TYPE_CHECKING:
    from homeassistant.components.assist_pipeline import Pipeline


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """The parts of an Assist pipeline Hearthkeeper reads."""

    id: str
    name: str
    language: str
    conversation_engine: str
    conversation_language: str
    stt_engine: str | None
    stt_language: str | None
    tts_engine: str | None
    tts_language: str | None
    tts_voice: str | None

    @property
    def label(self) -> str:
        """Return a human readable label for selectors."""
        return f"{self.name} ({self.language})"


def _to_settings(pipeline: Pipeline) -> PipelineSettings:
    """Convert a core pipeline into the settings we use."""
    language = pipeline.language or pipeline.conversation_language
    return PipelineSettings(
        id=pipeline.id,
        name=pipeline.name,
        language=language or DEFAULT_LANGUAGE,
        conversation_engine=pipeline.conversation_engine,
        conversation_language=pipeline.conversation_language or language,
        stt_engine=pipeline.stt_engine,
        stt_language=pipeline.stt_language,
        tts_engine=pipeline.tts_engine,
        tts_language=pipeline.tts_language,
        tts_voice=pipeline.tts_voice,
    )


def async_list_pipelines(hass: HomeAssistant) -> list[PipelineSettings]:
    """Return every stored pipeline, empty when Assist is not set up."""
    from homeassistant.components import assist_pipeline

    try:
        pipelines = assist_pipeline.async_get_pipelines(hass)
    except KeyError:
        # assist_pipeline is not loaded (yet). The options flow then offers
        # only "no pipeline" and setup waits for the dependency.
        return []
    return [_to_settings(pipeline) for pipeline in pipelines]


def async_get_pipeline_settings(
    hass: HomeAssistant, pipeline_id: str
) -> PipelineSettings:
    """Return one pipeline.

    Raises ``KeyError`` when Assist is not loaded and ``PipelineNotFound``
    when the id does not exist.
    """
    from homeassistant.components import assist_pipeline

    return _to_settings(assist_pipeline.async_get_pipeline(hass, pipeline_id))


def extract_speech(response: Any) -> str:
    """Return the plain speech from a conversation result."""
    if not isinstance(response, dict):
        return ""
    result = response.get("response")
    if not isinstance(result, dict):
        return ""
    speech = result.get("speech")
    if not isinstance(speech, dict):
        return ""
    plain = speech.get("plain")
    if not isinstance(plain, dict):
        return ""
    text = plain.get("speech")
    return text.strip() if isinstance(text, str) else ""
