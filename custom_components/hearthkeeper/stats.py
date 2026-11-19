"""Runtime counters shown by diagnostics.

Only numbers and error class names live here: no message texts, no chat ids
and nothing that could reconstruct a conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.util import dt as dt_util


@dataclass(slots=True)
class RuntimeStats:
    """Counters that live as long as the config entry."""

    text: int = 0
    commands: int = 0
    callbacks: int = 0
    voice_in: int = 0
    voice_out: int = 0
    briefings: int = 0
    skills: int = 0
    reminders: int = 0
    errors: int = 0
    last_error: str | None = None
    last_error_at: str | None = None

    def record_error(self, kind: str) -> None:
        """Remember that an operation failed, by class name only."""
        self.errors += 1
        self.last_error = kind
        self.last_error_at = dt_util.utcnow().isoformat()

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics representation."""
        return {
            "text": self.text,
            "commands": self.commands,
            "callbacks": self.callbacks,
            "voice_in": self.voice_in,
            "voice_out": self.voice_out,
            "briefings": self.briefings,
            "skills": self.skills,
            "reminders": self.reminders,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }


__all__ = ["RuntimeStats"]
