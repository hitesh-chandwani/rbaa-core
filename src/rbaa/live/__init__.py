"""Gemini Live session client (#11): one bidirectional-audio WebSocket session per agent.

`GeminiLiveSession` speaks the raw Gemini Live `BidiGenerateContent` WebSocket protocol directly
via the `websockets` library (not through `google-genai`'s own `client.aio.live.connect`
wrapper -- that one isn't designed to be pointed at a local fake server, which the testing
guidelines require here). The wire message shapes (`setup`, `realtimeInput`, `clientContent`,
`toolResponse` outgoing; `setupComplete`, `serverContent`, `toolCall`, `sessionResumptionUpdate`
incoming) are confirmed against the installed `google-genai` SDK's `types` module (its pydantic
field aliases are the camelCase wire keys) and are used here as *serialization/parsing helpers
only* -- the transport itself is plain `websockets`, per the issue's Constraints.

Per the PM's groomed clarification, this module depends only on #1: `connect()` takes a plain
`system_instruction: str` the caller has already built. It never imports `Role` or `WorkContext`.

Architecture
------------
A single background task (`_receive_loop`), started by `connect()` and running for the lifetime of
the session (including across reconnects), is the *sole* reader of the WebSocket. It parses each
incoming message and either:

- stores the session-resumption handle,
- pushes a decoded 24 kHz PCM16 chunk onto an internal `_audio_queue`,
- pushes a `LiveEvent` onto an internal `_event_queue`, or
- on an `interrupted` signal, synchronously drains `_audio_queue` (discarding anything already
  read off the socket for the interrupted turn but not yet handed to a consumer) before pushing
  exactly one `Interrupted()` -- see `_handle_server_content`'s docstring for why this drain is
  race-free with respect to a concurrent `audio_out()` consumer.

`audio_out()` and `events()` are thin async generators that just pop off those two queues; they
never touch the socket directly. This is what lets a dropped connection be transparently retried
(see `_attempt_reconnect`, filled in later) without either generator needing to know.

Judgment calls are called out explicitly, close to the code they affect, and summarized in the
engineer's issue comment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from google.genai import types
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_LIVE_MODEL = "gemini-3.8-live"

# Real Gemini Live endpoint (api-key auth), matching the google-genai SDK's own construction of
# `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.<version>.GenerativeService.BidiGenerateContent`.
# Tests always override `ws_url` to point at a local fake server instead.
DEFAULT_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

# 16 kHz PCM in / 24 kHz PCM out, per design.md §1.
INPUT_SAMPLE_RATE_HZ = 16000
OUTPUT_SAMPLE_RATE_HZ = 24000

# Reconnection shape pinned by #11: up to 3 attempts, a fixed 1s delay, injectable sleep.
RECONNECT_MAX_ATTEMPTS = 3
RECONNECT_DELAY_SECONDS = 1.0

# A sentinel pushed onto both internal queues by close() so audio_out()/events() stop iterating
# instead of hanging forever on an empty queue.
_CLOSE_SENTINEL = object()

SleepFn = Callable[[float], Awaitable[None]]


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
# through verbatim into the setup message's `tools` field -- see `connect`'s docstring for why a
# plain dict (no validation/normalization) was chosen.
ToolDeclaration = dict[str, Any]


def _resolve_model(model: str | None) -> str:
    """Precedence pinned by #11 (matching #10's pattern): explicit `model` param > the
    `GEMINI_LIVE_MODEL` env var > `DEFAULT_GEMINI_LIVE_MODEL`.

    Judgment call: `gemini-3.8-live` (design.md §1's choice) was confirmed as a current, real
    Gemini Live model id against https://ai.google.dev/gemini-api/docs/models on 2026-10-04, the
    same way #10 confirmed its own text-model default.
    """
    if model is not None:
        return model
    return os.environ.get("GEMINI_LIVE_MODEL", DEFAULT_GEMINI_LIVE_MODEL)


class GeminiLiveSession:
    """One Gemini Live bidirectional-audio WebSocket session. See the module docstring for the
    overall architecture and the issue for the full pinned contract.

    Judgment call: `__init__` takes one extra keyword-only parameter beyond the two the issue
    pins, `sleep`. The issue's "Public API" block pins `model` and `ws_url` by name but does not
    mention a `sleep` hook there, while its own acceptance criteria require "an injectable sleep
    function ... so a test can pass a fake clock/no-op sleep". Those two parts of the same issue
    are not simultaneously satisfiable without adding a parameter, so a constructor-level
    `sleep: Callable[[float], Awaitable[None]] | None = None` (default `asyncio.sleep`) was added;
    every test that only names `model`/`ws_url` is unaffected since it is optional and keyword-only.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        ws_url: str | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._model = _resolve_model(model)
        self._ws_url = ws_url or DEFAULT_WS_URL
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep

        self._system_instruction: str | None = None
        self._voice: str | None = None
        self._tools: list[ToolDeclaration] = []

        self._api_key: str = ""  # set in connect(); never logged, never in repr().
        self._ws: Any = None
        self._resumption_handle: str | None = None

        self._audio_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._event_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._pending_tool_call_names: dict[str, str] = {}

        self._receive_task: asyncio.Task[None] | None = None
        self._setup_complete_event = asyncio.Event()
        self._closing = False
        self._closed = False

    def __repr__(self) -> str:
        # Never include the API key (or anything derived from it) here.
        return f"GeminiLiveSession(model={self._model!r}, ws_url={self._ws_url!r})"

    # ------------------------------------------------------------------
    # connect / close
    # ------------------------------------------------------------------

    async def connect(
        self, *, system_instruction: str, voice: str, tools: list[ToolDeclaration] | None = None
    ) -> None:
        """Open the session: connect the socket, send the `setup` message, and wait for
        `setupComplete`. Starts the single background `_receive_loop` task that stays alive for
        the rest of this session's life (including across reconnects).

        Sends `response_modalities=["AUDIO"]`, both input and output transcription enabled,
        `session_resumption` (an empty handle to start) and `context_window_compression` (a
        sliding window), per design.md §3 and #11's acceptance criteria.
        """
        self._system_instruction = system_instruction
        self._voice = voice
        self._tools = list(tools) if tools else []
        self._api_key = os.environ.get("GOOGLE_API_KEY", "")

        self._closing = False
        self._closed = False
        self._setup_complete_event = asyncio.Event()

        self._ws = await self._open_socket()
        await self._ws.send(json.dumps({"setup": self._build_setup_dict(resumption_handle=None)}))

        self._receive_task = asyncio.create_task(self._receive_loop())
        await asyncio.wait_for(self._setup_complete_event.wait(), timeout=10)

    async def close(self) -> None:
        """Close the WebSocket cleanly, end `audio_out()`/`events()` iteration, and do nothing
        (not raise) if called again."""
        if self._closed:
            return
        self._closed = True
        self._closing = True

        if self._receive_task is not None:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        await self._audio_queue.put(_CLOSE_SENTINEL)
        await self._event_queue.put(_CLOSE_SENTINEL)

    # ------------------------------------------------------------------
    # outgoing messages (filled in by later commits)
    # ------------------------------------------------------------------

    async def send_audio(self, pcm_16khz: bytes) -> None:
        raise NotImplementedError

    async def say(self, script: str) -> None:
        raise NotImplementedError

    async def send_tool_response(self, call_id: str, result: dict) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # incoming streams (filled in by later commits)
    # ------------------------------------------------------------------

    def audio_out(self) -> AsyncIterator[bytes]:
        raise NotImplementedError

    def events(self) -> AsyncIterator[LiveEvent]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # wire protocol helpers
    # ------------------------------------------------------------------

    async def _open_socket(self):
        headers = {"x-goog-api-key": self._api_key} if self._api_key else {}
        return await websockets.connect(self._ws_url, additional_headers=headers)

    def _build_setup_dict(self, *, resumption_handle: str | None) -> dict[str, Any]:
        """Build the `LiveClientSetup` wire dict using `google-genai`'s `types` as a
        serialization helper (per the issue's Constraints: used for types/schema only, not
        transport). `tools` is deliberately NOT passed through this model -- see the module-level
        `ToolDeclaration` docstring.
        """
        setup = types.LiveClientSetup(
            model=self._model,
            system_instruction=types.Content(parts=[types.Part(text=self._system_instruction)]),
            generation_config=types.GenerationConfig(
                response_modalities=[types.Modality.AUDIO],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                    )
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        )
        setup_dict = setup.model_dump(mode="json", by_alias=True, exclude_none=True)
        if self._tools:
            setup_dict["tools"] = [{"functionDeclarations": self._tools}]
        return setup_dict

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("GeminiLiveSession is not connected")
        await self._ws.send(json.dumps(payload))

    async def _receive_loop(self) -> None:
        """The single, lifetime-of-the-session reader of the WebSocket. Dispatches each message;
        on an unexpected close, hands off to reconnection (filled in by a later commit -- for now
        it just stops)."""
        while True:
            try:
                raw = await self._ws.recv()
            except ConnectionClosed:
                return
            await self._dispatch(raw)

    async def _dispatch(self, raw: str | bytes) -> None:
        data = json.loads(raw)
        if "setupComplete" in data:
            self._setup_complete_event.set()
            return
        # serverContent/toolCall/sessionResumptionUpdate handling arrives in later commits.


__all__ = [
    "DEFAULT_GEMINI_LIVE_MODEL",
    "DEFAULT_WS_URL",
    "INPUT_SAMPLE_RATE_HZ",
    "OUTPUT_SAMPLE_RATE_HZ",
    "RECONNECT_MAX_ATTEMPTS",
    "RECONNECT_DELAY_SECONDS",
    "Interrupted",
    "ToolCall",
    "TranscriptUpdate",
    "SessionLost",
    "LiveEvent",
    "ToolDeclaration",
    "SleepFn",
    "GeminiLiveSession",
]
