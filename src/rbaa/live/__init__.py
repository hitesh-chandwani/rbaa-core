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

Transcripts: the real API's transcription chunks never carry a `finished` field (see
`_handle_server_content`'s docstring for the real-API bug this caused and its fix) -- text is
accumulated across chunks per turn and finalized (`is_final=True`, full accumulated text) only on
a later, separate `generation_complete`/`turn_complete` signal; non-final, running-text
`TranscriptUpdate`s are also pushed per chunk along the way.

`audio_out()` and `events()` are thin async generators that just pop off those two queues; they
never touch the socket directly. This is what lets a dropped connection be transparently retried
(see `_attempt_reconnect`, filled in later) without either generator needing to know.

Judgment calls are called out explicitly, close to the code they affect, and summarized in the
engineer's issue comment.
"""

from __future__ import annotations

import asyncio
import base64
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

# The `websockets` library itself logs the full HTTP handshake request/response, headers
# included, at DEBUG level -- and our `x-goog-api-key` header is one of those headers. An
# embedding application configuring its root logger at DEBUG would otherwise leak the API key
# through `websockets`'s own logger, bypassing every precaution this module takes with its own
# `logger` above. Setting these two loggers' own level explicitly wins over an inherited root
# level (Python's logging effective-level lookup stops at the first ancestor with its own level
# set), so this holds regardless of how the embedding application configures the root logger.
# Found via this module's own `GOOGLE_API_KEY`-never-logged test, which failed without this.
logging.getLogger("websockets.client").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)

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

# say()'s exact request wording -- a module constant so a test can assert on it precisely. See
# say()'s docstring for the judgment call this pins.
_SAY_INSTRUCTION_TEMPLATE = (
    "Say the following verbatim, with no changes, additions or omissions: %s"
)

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


def _model_wire_name(model: str) -> str:
    """The real `generativelanguage.googleapis.com` BidiGenerateContent endpoint rejects the
    `setup` message's `model` field unless it is prefixed `models/` (matching the REST API's
    `model.name` format, e.g. `models/gemini-3.8-live`, confirmed via `GET .../v1beta/models`) --
    a bare id like `gemini-3.8-live` gets a 1007 close with "unexpected model name format".
    `GEMINI_LIVE_MODEL`/the constructor's `model` param keep holding the bare id (consistent with
    how model ids are referenced elsewhere, e.g. #10's `GEMINI_TEXT_MODEL`); this prefixes only at
    the point of building the wire payload, and does not double-prefix a caller who already passes
    `models/...`.
    """
    if model.startswith("models/"):
        return model
    return f"models/{model}"


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

        # Running per-turn transcript accumulators -- see `_handle_server_content` for why these
        # exist (the real API's `finished` field on a transcription chunk is fictional) and the
        # module docstring's "Real-API protocol bug (#11 follow-up)" section for the full story.
        self._output_transcript_buffer: str = ""
        self._input_transcript_buffer: str = ""

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
        self._output_transcript_buffer = ""
        self._input_transcript_buffer = ""

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
        """Send raw 16 kHz, mono, 16-bit little-endian PCM (no WAV header) as a `realtimeInput`
        message.

        Judgment call / bug found and fixed: `google-genai`'s own pydantic `model_dump(mode=
        "json")` for a `bytes` field (e.g. `Blob.data`) encodes using the URL-safe base64 alphabet
        (`-`/`_`, no padding), not the standard base64 alphabet (`+`/`/`, padded) that both the
        real Gemini Live wire protocol (proto3 JSON's `bytes` mapping) and this module's fake test
        server expect. Confirmed by direct experiment: encoding `bytes(range(256)) * 4` through
        `Blob(data=...).model_dump(mode="json")` and decoding the result with standard
        `base64.b64decode` silently produces *different, shorter* bytes than the original -- a
        real audio-corruption bug that small test buffers happened not to exercise (no byte
        triggered the `+`/`/` vs `-`/`_` difference). So the `data` field is base64-encoded here
        by hand with the standard alphabet instead of trusting the model's own dump for it;
        `google-genai`'s types are still used for every other (non-bytes) field.
        """
        payload = {
            "realtimeInput": {
                "audio": {
                    "data": base64.b64encode(pcm_16khz).decode("ascii"),
                    "mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE_HZ}",
                }
            }
        }
        await self._send_json(payload)

    async def say(self, script: str) -> None:
        """Make the model speak `script` verbatim: a `clientContent` turn whose single text part
        instructs the model to read `script` exactly, with `turn_complete=True` so the model
        responds immediately rather than waiting for more input.

        Judgment call: the issue leaves the exact instruction wording unpinned ("e.g. a turn with
        the script as input text and an instruction to read it exactly ... pin the exact request
        shape at implementation time"). The wording below was chosen for clarity to the model;
        `_SAY_INSTRUCTION_TEMPLATE` is a module-level constant so a test can assert on it exactly
        without re-deriving it independently.
        """
        msg = types.LiveClientMessage(
            client_content=types.LiveClientContent(
                turns=[
                    types.Content(
                        role="user", parts=[types.Part(text=_SAY_INSTRUCTION_TEMPLATE % script)]
                    )
                ],
                turn_complete=True,
            )
        )
        await self._send_json(msg.model_dump(mode="json", by_alias=True, exclude_none=True))

    async def send_tool_response(self, call_id: str, result: dict) -> None:
        """Send a `toolResponse` keyed by `call_id`, carrying `result` as its `response`. `name`
        is filled in from the matching `ToolCall` dispatched earlier (tracked in
        `_pending_tool_call_names`) when available -- the real Gemini Live API documents
        `FunctionResponse.name`, but #11's acceptance criterion only requires the id and result to
        round-trip, so a response for an id this session never saw a `ToolCall` for still sends
        (with no `name`) rather than raising."""
        name = self._pending_tool_call_names.pop(call_id, None)
        msg = types.LiveClientMessage(
            tool_response=types.LiveClientToolResponse(
                function_responses=[types.FunctionResponse(id=call_id, name=name, response=result)]
            )
        )
        await self._send_json(msg.model_dump(mode="json", by_alias=True, exclude_none=True))

    # ------------------------------------------------------------------
    # incoming streams
    # ------------------------------------------------------------------

    async def audio_out(self) -> AsyncIterator[bytes]:
        """Yield decoded 24 kHz, mono, 16-bit little-endian PCM chunks as they arrive. Ends
        (`StopAsyncIteration`) once `close()` has been called and no more chunks are queued.
        Never sees chunks that were discarded for an interrupted turn -- see
        `_handle_server_content`."""
        while True:
            item = await self._audio_queue.get()
            if item is _CLOSE_SENTINEL:
                return
            yield item

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Yield `LiveEvent`s (`Interrupted`, `ToolCall`, `TranscriptUpdate`, `SessionLost`) as
        they occur. Ends (`StopAsyncIteration`) once `close()` has been called and no more events
        are queued."""
        while True:
            item = await self._event_queue.get()
            if item is _CLOSE_SENTINEL:
                return
            yield item

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
            model=_model_wire_name(self._model),
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
        on an unexpected close (anything other than our own `close()`), hands off to
        `_attempt_reconnect`. If that succeeds, keeps reading from the new socket; if it is
        exhausted, emits exactly one `SessionLost` and stops -- the session does not retry again
        on its own.
        """
        while True:
            try:
                raw = await self._ws.recv()
            except (ConnectionClosed, OSError):
                if self._closing:
                    return
                reconnected = await self._attempt_reconnect()
                if not reconnected:
                    await self._event_queue.put(
                        SessionLost(
                            reason=f"reconnect failed after {RECONNECT_MAX_ATTEMPTS} attempts"
                        )
                    )
                    return
                continue
            await self._dispatch(raw)

    async def _attempt_reconnect(self) -> bool:
        """Try up to `RECONNECT_MAX_ATTEMPTS` times, with a fixed `RECONNECT_DELAY_SECONDS` delay
        (via the injectable `self._sleep`, default `asyncio.sleep`) before each attempt, to open a
        new socket and resume the session using the last stored resumption handle. Returns True
        and swaps in the new socket on the first successful attempt; returns False once every
        attempt has failed.

        Judgment call: the delay is applied before *every* attempt, including the first (rather
        than only *between* attempts, i.e. none before the first) -- the issue says "a fixed
        1-second delay between attempts" without pinning whether attempt 1 is immediate. Applying
        it uniformly is simpler to implement and to assert on (`sleep` is called exactly
        `RECONNECT_MAX_ATTEMPTS` times), and 3 * 1s of delay plus near-instant local connection
        attempts still comfortably clears the "well inside 5 seconds" budget the issue requires.
        """
        for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
            await self._sleep(RECONNECT_DELAY_SECONDS)
            try:
                new_ws = await self._open_socket()
                await new_ws.send(
                    json.dumps(
                        {"setup": self._build_setup_dict(resumption_handle=self._resumption_handle)}
                    )
                )
                raw = await asyncio.wait_for(new_ws.recv(), timeout=5)
                if "setupComplete" not in json.loads(raw):
                    raise RuntimeError("reconnect: did not receive setupComplete")
            except Exception:
                logger.warning(
                    "GeminiLiveSession reconnect attempt %d/%d failed",
                    attempt,
                    RECONNECT_MAX_ATTEMPTS,
                )
                continue
            self._ws = new_ws
            return True
        return False

    async def _dispatch(self, raw: str | bytes) -> None:
        data = json.loads(raw)
        if "setupComplete" in data:
            self._setup_complete_event.set()
            return

        # `types.LiveServerMessage.model_validate` is used purely as a parsing helper: it base64-
        # decodes `inlineData.data` (audio) into real `bytes` for us, and gives named access to
        # every field by its snake_case name regardless of the wire's camelCase key.
        message = types.LiveServerMessage.model_validate(data)

        if message.server_content is not None:
            await self._handle_server_content(message.server_content)
        if message.tool_call is not None:
            await self._handle_tool_call(message.tool_call)
        if message.session_resumption_update is not None:
            new_handle = message.session_resumption_update.new_handle
            if new_handle:
                self._resumption_handle = new_handle

    async def _handle_server_content(self, content: types.LiveServerContent) -> None:
        """Handle one `serverContent` message: barge-in, audio chunks, transcripts.

        Race-freedom of the interrupted drain: `_receive_loop` is the *only* task that ever calls
        `_audio_queue.put`/`get_nowait`, and this method runs to completion with no `await`
        between "decide `interrupted` is set" and "drain the queue and push `Interrupted()`" --
        asyncio only switches tasks at an `await` point, so a concurrent `audio_out()` consumer
        can never observe the queue mid-drain. Anything already sitting in the queue at the
        instant this message is processed (i.e. read off the socket for the interrupted turn but
        not yet popped by `audio_out()`) is removed before any `audio_out()` consumer can see it.

        Empty `serverContent` messages (no `model_turn`, no transcription, `interrupted` falsy,
        neither `generation_complete` nor `turn_complete` set -- observed on the real API as
        presumably pure audio-prep chunks) fall through every branch below untouched: a no-op, as
        expected.

        Real-API protocol bug fix (#11 follow-up, found testing against the live API): the real
        server's `outputTranscription`/`inputTranscription` chunks **never** carry a `finished`
        field -- it is fictional for this API, and reading it (as this method used to) means
        `is_final` can never become `True` in production. Turn completion is instead signalled by
        a later, *separate* `serverContent` message carrying `generation_complete` and/or
        `turn_complete`, with no transcription payload at all. The real captured sequence for one
        `say()` call was: several transcription chunks with no `finished` key, interleaved with
        pure-audio `model_turn` chunks, followed by `{"generationComplete": true}` and then
        `{"turnComplete": true}` as two separate messages carrying nothing else.

        Fix: accumulate `text` across chunks into `_output_transcript_buffer`/
        `_input_transcript_buffer` (independently for output/input), and finalize -- emit exactly
        one `TranscriptUpdate(text=<accumulated>, is_final=True)` per buffer that saw any text
        this turn, then reset that buffer -- on whichever of `generation_complete` or
        `turn_complete` arrives first. `turn_complete` is the one guaranteed to always fire: the
        real API's own field docs note an interrupted turn never gets a `generation_complete`, it
        goes `interrupted` -> `turn_complete` directly. Finalizing on the first signal and
        resetting the buffer means a later second signal for the same turn (e.g. `turn_complete`
        arriving after `generation_complete` already finalized it) finds an empty buffer and emits
        nothing more.

        Judgment call: an `interrupted` message does *not* reset these buffers. Unlike queued
        audio (which is discarded because it was never played and belongs to a turn the caller was
        told to stop), text already accumulated before the interruption is genuinely what the
        model said before being cut off, and the real API still sends a `turn_complete` for that
        same (now-interrupted) turn afterwards -- so the existing accumulated text is finalized by
        that `turn_complete` as the turn's (partial) transcript, not discarded.

        Judgment call: in addition to the one final event per turn, a non-final
        `TranscriptUpdate(is_final=False)` is also emitted per chunk as it arrives, carrying the
        *running* accumulated text so far (not just that chunk) -- matching this module's own
        Goal wording ("yields ... a running transcript") and useful for a live UI that wants
        growing text rather than disjoint fragments. These are purely additive: a caller that only
        wants the final transcript (e.g. `say()`) just waits for `is_final=True`, exactly as
        before this fix.
        """
        if content.interrupted:
            while True:
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await self._event_queue.put(Interrupted())

        if content.model_turn is not None:
            for part in content.model_turn.parts or []:
                if part.inline_data is not None and part.inline_data.data is not None:
                    await self._audio_queue.put(part.inline_data.data)

        output_transcription = content.output_transcription
        if output_transcription is not None and output_transcription.text is not None:
            self._output_transcript_buffer += output_transcription.text
            await self._event_queue.put(
                TranscriptUpdate(text=self._output_transcript_buffer, is_final=False)
            )
        input_transcription = content.input_transcription
        if input_transcription is not None and input_transcription.text is not None:
            self._input_transcript_buffer += input_transcription.text
            await self._event_queue.put(
                TranscriptUpdate(text=self._input_transcript_buffer, is_final=False)
            )

        if content.generation_complete or content.turn_complete:
            if self._output_transcript_buffer:
                await self._event_queue.put(
                    TranscriptUpdate(text=self._output_transcript_buffer, is_final=True)
                )
                self._output_transcript_buffer = ""
            if self._input_transcript_buffer:
                await self._event_queue.put(
                    TranscriptUpdate(text=self._input_transcript_buffer, is_final=True)
                )
                self._input_transcript_buffer = ""

    async def _handle_tool_call(self, tool_call: types.LiveServerToolCall) -> None:
        """Dispatch one `ToolCall` event per function call the server asked us to make, and
        remember each call's `name` so `send_tool_response` can fill it in later."""
        for call in tool_call.function_calls or []:
            if call.id is None or call.name is None:
                continue  # malformed; nothing we can round-trip a response against
            self._pending_tool_call_names[call.id] = call.name
            await self._event_queue.put(ToolCall(id=call.id, name=call.name, args=call.args or {}))


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
