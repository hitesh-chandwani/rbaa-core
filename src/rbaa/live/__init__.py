"""Gemini Live session client (#11): one bidirectional-audio WebSocket session per agent.

See the issue's groomed body for the full pinned public API and acceptance criteria. This module
is implemented incrementally; see the docstrings on `GeminiLiveSession`'s methods as they are
filled in for the wire-protocol details and judgment calls.

Per the PM's groomed clarification, this module depends only on #1: `connect()` takes a plain
`system_instruction: str` the caller has already built. It must never import `Role` or
`WorkContext`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

DEFAULT_GEMINI_LIVE_MODEL = "gemini-3.8-live"

# Real Gemini Live endpoint (api-key auth, matching the google-genai SDK's own construction of
# `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.<version>.GenerativeService.BidiGenerateContent`).
# Tests always override `ws_url` to point at a local fake server instead.
DEFAULT_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

# 16 kHz PCM in / 24 kHz PCM out, per design.md §1.
INPUT_SAMPLE_RATE_HZ = 16000
OUTPUT_SAMPLE_RATE_HZ = 24000


@dataclass(frozen=True)
class Interrupted:
    """Barge-in: the server told us to stop -- see `events()`."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class TranscriptUpdate:
    text: str
    is_final: bool


@dataclass(frozen=True)
class SessionLost:
    reason: str


LiveEvent = Interrupted | ToolCall | TranscriptUpdate | SessionLost

# A single Gemini Live function-declaration JSON object (name/description/parameters). Passed
# through verbatim into the setup message's `tools` field -- see `connect`'s docstring once
# implemented for why a plain dict (no validation/normalization) was chosen.
ToolDeclaration = dict[str, Any]


def _resolve_model(model: str | None) -> str:
    """Precedence pinned by #11 (matching #10's pattern): explicit `model` param > the
    `GEMINI_LIVE_MODEL` env var > `DEFAULT_GEMINI_LIVE_MODEL`."""
    if model is not None:
        return model
    return os.environ.get("GEMINI_LIVE_MODEL", DEFAULT_GEMINI_LIVE_MODEL)


class GeminiLiveSession:
    """One Gemini Live bidirectional-audio WebSocket session. See the module docstring and the
    issue for the full contract. Methods below are being implemented incrementally."""

    def __init__(self, *, model: str | None = None, ws_url: str | None = None) -> None:
        self._model = _resolve_model(model)
        self._ws_url = ws_url or DEFAULT_WS_URL

    def __repr__(self) -> str:
        # Never include the API key (or anything derived from it) here.
        return f"GeminiLiveSession(model={self._model!r}, ws_url={self._ws_url!r})"

    async def connect(
        self, *, system_instruction: str, voice: str, tools: list[ToolDeclaration] | None = None
    ) -> None:
        raise NotImplementedError

    async def send_audio(self, pcm_16khz: bytes) -> None:
        raise NotImplementedError

    def audio_out(self):
        raise NotImplementedError

    def events(self):
        raise NotImplementedError

    async def say(self, script: str) -> None:
        raise NotImplementedError

    async def send_tool_response(self, call_id: str, result: dict) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


__all__ = [
    "DEFAULT_GEMINI_LIVE_MODEL",
    "DEFAULT_WS_URL",
    "INPUT_SAMPLE_RATE_HZ",
    "OUTPUT_SAMPLE_RATE_HZ",
    "Interrupted",
    "ToolCall",
    "TranscriptUpdate",
    "SessionLost",
    "LiveEvent",
    "ToolDeclaration",
    "GeminiLiveSession",
]
