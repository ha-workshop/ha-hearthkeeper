"""Tests for voice helpers."""

from __future__ import annotations

import io
import shutil
import sys
import types
import wave
from pathlib import Path
from typing import Any

import homeassistant.components as ha_components
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.hearthkeeper import voice
from custom_components.hearthkeeper.assist import PipelineSettings
from custom_components.hearthkeeper.transport import Attachment
from custom_components.hearthkeeper.voice import (
    PipelineReply,
    VoiceTooLongError,
    async_run_audio_pipeline,
    async_transcode_to_wav,
    audio_suffix,
    is_audio,
)

PIPELINE = PipelineSettings(
    id="pipeline-1",
    name="Home",
    language="ru",
    conversation_engine="conversation.home",
    conversation_language="ru-RU",
    stt_engine="stt.faster_whisper",
    stt_language="ru",
    tts_engine="tts.piper",
    tts_language="ru",
    tts_voice="ru_RU-denis-medium",
)


def test_suffix_from_mime() -> None:
    """A voice note keeps its OGG container."""
    assert audio_suffix(Attachment("f", "audio/ogg")) == ".ogg"
    assert audio_suffix(Attachment("f", "audio/mpeg")) == ".mp3"
    assert audio_suffix(Attachment("f", "audio/ogg; codecs=opus")) == ".ogg"


def test_suffix_from_file_name() -> None:
    """Documents carry a name instead of a useful MIME type."""
    assert (
        audio_suffix(Attachment("f", "application/octet-stream", file_name="a.ogg"))
        == ".ogg"
    )
    assert audio_suffix(Attachment("f", None, file_name="note.M4A")) == ".m4a"
    assert audio_suffix(Attachment("f", "", file_name="memo.opus")) == ".ogg"


def test_non_audio_attachments() -> None:
    """Photos and unknown files are not routed to speech recognition."""
    assert audio_suffix(None) is None
    assert audio_suffix(Attachment("f", "image/jpeg")) is None
    assert audio_suffix(Attachment("f", "application/pdf", file_name="a.pdf")) is None
    assert not is_audio(Attachment("f", "video/mp4"))


async def test_transcode_rejects_long_audio(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long voice note is refused before decoding."""

    async def _duration(*args: Any, **kwargs: Any) -> float:
        return 300.0

    monkeypatch.setattr(voice, "_async_duration", _duration)

    with pytest.raises(VoiceTooLongError):
        await async_transcode_to_wav(hass, Path("/tmp/a.ogg"), max_seconds=120)


async def test_transcode_without_ffmpeg(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ffmpeg is reported as a normal failure."""

    async def _duration(*args: Any, **kwargs: Any) -> None:
        return None

    async def _which(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(voice, "_async_duration", _duration)
    monkeypatch.setattr(voice, "_async_which", _which)

    with pytest.raises(HomeAssistantError):
        await async_transcode_to_wav(hass, Path("/tmp/a.ogg"), max_seconds=120)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
async def test_transcode_produces_16k_mono_wav(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Decoding normalises any input to WAV, 16 kHz, mono."""
    source = tmp_path / "voice.wav"
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 2 * 8000)
    source.write_bytes(buffer.getvalue())

    result = await async_transcode_to_wav(hass, source, max_seconds=60)

    assert result[:4] == b"RIFF"
    assert int.from_bytes(result[22:24], "little") == 1  # mono
    assert int.from_bytes(result[24:28], "little") == 16000
    assert int.from_bytes(result[34:36], "little") == 16  # bits per sample


def _fake_pipeline_module(calls: list[dict[str, Any]]) -> types.ModuleType:
    """Build a stand-in for the Assist pipeline package."""
    module = types.ModuleType("homeassistant.components.assist_pipeline")

    class _EventType:
        STT_END = "stt-end"
        INTENT_END = "intent-end"

    class _Stage:
        STT = "stt"
        INTENT = "intent"

    async def _run(hass: HomeAssistant, **kwargs: Any) -> None:
        calls.append(kwargs)
        callback = kwargs["event_callback"]
        callback(
            types.SimpleNamespace(
                type="stt-end", data={"stt_output": {"text": "включи свет"}}
            )
        )
        callback(
            types.SimpleNamespace(
                type="intent-end",
                data={
                    "intent_output": {
                        "response": {"speech": {"plain": {"speech": " Включаю "}}},
                        "conversation_id": "tg:42:voice",
                    }
                },
            )
        )

    module.PipelineEventType = _EventType  # type: ignore[attr-defined]
    module.PipelineStage = _Stage  # type: ignore[attr-defined]
    module.PipelineEvent = object  # type: ignore[attr-defined]
    module.async_pipeline_from_audio_stream = _run  # type: ignore[attr-defined]
    return module


async def test_audio_pipeline_returns_speech(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline reply and its conversation id are collected from events."""
    calls: list[dict[str, Any]] = []
    fake = _fake_pipeline_module(calls)
    monkeypatch.setitem(sys.modules, "homeassistant.components.assist_pipeline", fake)
    monkeypatch.setattr(ha_components, "assist_pipeline", fake, raising=False)

    reply = await async_run_audio_pipeline(
        hass,
        pipeline=PIPELINE,
        conversation_id="tg:42",
        wav=b"\x00\x01" * 8000,
        language="ru",
    )

    assert reply == PipelineReply(text="Включаю", conversation_id="tg:42:voice")
    call = calls[0]
    assert call["pipeline_id"] == "pipeline-1"
    assert call["conversation_id"] == "tg:42"
    assert call["start_stage"] == "stt"
    assert call["end_stage"] == "intent"
    metadata = call["stt_metadata"]
    assert metadata.language == "ru"
    assert metadata.format.value == "wav"
    assert metadata.sample_rate == 16000
    assert metadata.channel == 1


async def test_audio_pipeline_requires_stt(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline without STT is rejected before importing Assist."""
    no_stt = PipelineSettings(
        id="p2",
        name="No STT",
        language="ru",
        conversation_engine="conversation.home",
        conversation_language="ru",
        stt_engine=None,
        stt_language=None,
        tts_engine=None,
        tts_language=None,
        tts_voice=None,
    )

    with pytest.raises(HomeAssistantError):
        await async_run_audio_pipeline(
            hass, pipeline=no_stt, conversation_id="tg:42", wav=b"", language="ru"
        )
