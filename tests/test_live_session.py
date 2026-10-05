"""Tests for `rbaa.live.GeminiLiveSession` (#11).

Every test in the default suite runs against the in-process `FakeLiveServer`
(`tests/fake_live_server.py`) -- no real network call, no real `GOOGLE_API_KEY` needed. The two
genuinely-live tests (a 20-minute keepalive, and a 9/10 `say()` transcript match against the real
API) are `@pytest.mark.live` and skip themselves when `GOOGLE_API_KEY` is not set, matching #10's
pattern (`tests/test_standup_drafter.py::test_live_draft_standup_end_to_end`).

Mapping to #11's acceptance criteria (see the engineer's issue comment for the same mapping):

- `connect()` setup message shape ->
  `test_connect_sends_setup_with_system_instruction_voice_and_tools`
- `send_audio`/`audio_out` raw PCM, no WAV header, base64 on the wire only ->
  `test_send_audio_sends_raw_pcm_without_wav_header`, `test_audio_out_yields_decoded_pcm_chunks`
- interrupted discards stale, unyielded audio -> `test_interrupted_discards_stale_audio`
- session_resumption/context_window_compression fields sent; live 20-min keepalive ->
  `test_connect_sends_setup_with_system_instruction_voice_and_tools`,
  `test_live_session_survives_20_minutes`
- reconnection (success before 3, SessionLost after 3) ->
  `test_reconnect_succeeds_before_third_attempt`, `test_reconnect_gives_up_after_three_attempts`
- tool declarations / tool_call / send_tool_response round trip -> `test_tool_call_round_trip`
- say() request shape + transcript capture; live 9/10 match ->
  `test_say_sends_correct_request_and_captures_transcript`, `test_live_say_transcript_match_rate`
- GOOGLE_API_KEY never logged -> `test_api_key_never_appears_in_logs_exceptions_or_repr`
- model id precedence -> `test_model_id_precedence`
- close() idempotent, ends iteration -> `test_close_is_idempotent_and_ends_iteration`
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

import pytest
from fake_live_server import (
    DROP,
    EMPTY_SERVER_CONTENT_MESSAGE,
    GENERATION_COMPLETE_MESSAGE,
    TURN_COMPLETE_MESSAGE,
    FakeLiveServer,
    audio_chunk_message,
    tool_call_message,
    transcript_message,
    wait_until,
)

from rbaa.live import (
    DEFAULT_GEMINI_LIVE_MODEL,
    GeminiLiveSession,
    Interrupted,
    SessionLost,
    ToolCall,
    TranscriptUpdate,
)

FAKE_API_KEY = "FAKEKEY123"

TOOLS = [
    {
        "name": "get_ticket",
        "description": "Look up a Jira ticket by key.",
        "parameters": {"type": "OBJECT", "properties": {"key": {"type": "STRING"}}},
    }
]


@pytest.fixture
async def fake_server():
    server = FakeLiveServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _anext(aiter, *, timeout: float = 2.0):
    return await asyncio.wait_for(aiter.__anext__(), timeout=timeout)


# --------------------------------------------------------------------------------------------
# connect(): setup message shape
# --------------------------------------------------------------------------------------------


async def test_connect_sends_setup_with_system_instruction_voice_and_tools(fake_server):
    fake_server.plan_connection()
    session = GeminiLiveSession(ws_url=fake_server.ws_url)

    await session.connect(system_instruction="Be concise and factual.", voice="Kore", tools=TOOLS)
    try:
        setup = fake_server.connections[0].setup
        assert setup["systemInstruction"]["parts"][0]["text"] == "Be concise and factual."
        assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
        voice_config = setup["generationConfig"]["speechConfig"]["voiceConfig"]
        assert voice_config["prebuiltVoiceConfig"]["voiceName"] == "Kore"
        assert "inputAudioTranscription" in setup
        assert "outputAudioTranscription" in setup
        # Acceptance criterion 3: session resumption (empty handle to start) and context-window
        # compression (sliding window) are both enabled in the setup message.
        assert setup["sessionResumption"] == {}
        assert "slidingWindow" in setup["contextWindowCompression"]
        # Tool declarations are sent verbatim.
        assert setup["tools"] == [{"functionDeclarations": TOOLS}]
    finally:
        await session.close()


async def test_setup_model_field_has_models_prefix(fake_server):
    """Regression test: the real `generativelanguage.googleapis.com` BidiGenerateContent endpoint
    rejects the `setup` message with a 1007 close ("unexpected model name format") unless `model`
    is prefixed `models/`, matching the REST API's `model.name` format. `GEMINI_LIVE_MODEL`/the
    constructor's `model` param keep holding the bare id; only the wire payload is prefixed."""
    fake_server.plan_connection()
    session = GeminiLiveSession(ws_url=fake_server.ws_url)

    await session.connect(system_instruction="x", voice="Kore")
    try:
        assert fake_server.connections[0].setup["model"] == f"models/{DEFAULT_GEMINI_LIVE_MODEL}"
    finally:
        await session.close()


async def test_connect_without_tools_sends_no_tools_field(fake_server):
    fake_server.plan_connection()
    session = GeminiLiveSession(ws_url=fake_server.ws_url)

    await session.connect(system_instruction="x", voice="Kore")
    try:
        assert "tools" not in fake_server.connections[0].setup
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# send_audio / audio_out: raw PCM, no WAV header, base64 only on the wire
# --------------------------------------------------------------------------------------------


async def test_send_audio_sends_raw_pcm_without_wav_header(fake_server):
    received: list[dict] = []
    fake_server.plan_connection(on_message=lambda msg: received.append(msg) or None)
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore")

    pcm = bytes(range(256)) * 4  # arbitrary 16-bit-LE-shaped bytes; definitely not a WAV header
    try:
        await session.send_audio(pcm)
        await wait_until(lambda: len(received) == 1)

        audio = received[0]["realtimeInput"]["audio"]
        assert base64.b64decode(audio["data"]) == pcm
        assert audio["mimeType"] == "audio/pcm;rate=16000"
    finally:
        await session.close()


async def test_audio_out_yields_decoded_pcm_chunks(fake_server):
    chunk = b"\x11\x22\x33\x44" * 50
    fake_server.plan_connection(send=[audio_chunk_message(chunk, rate=24000)])
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore")

    try:
        received = await _anext(session.audio_out())
        assert received == chunk
        assert isinstance(received, bytes)
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# interrupted: discards audio already read off the socket but not yet yielded
# --------------------------------------------------------------------------------------------


async def test_interrupted_discards_stale_audio(fake_server):
    turn1_a, turn1_b = b"TURN1-CHUNK-A", b"TURN1-CHUNK-B"
    turn2_a, turn2_b = b"TURN2-CHUNK-A", b"TURN2-CHUNK-B"
    fake_server.plan_connection(
        send=[
            audio_chunk_message(turn1_a),
            audio_chunk_message(turn1_b),
            {"serverContent": {"interrupted": True}},
            audio_chunk_message(turn2_a),
            audio_chunk_message(turn2_b),
        ]
    )
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore")

    try:
        # Give the whole scripted burst time to arrive and be processed (including the drain)
        # before we start pulling from audio_out() -- see fake_live_server.py's module docstring
        # and #11's issue comment for why the drain itself is race-free regardless of this delay.
        await asyncio.sleep(0.1)

        audio_iter = session.audio_out()
        chunks = [await _anext(audio_iter), await _anext(audio_iter)]
        assert chunks == [turn2_a, turn2_b]
        assert turn1_a not in chunks
        assert turn1_b not in chunks

        event = await _anext(session.events())
        assert event == Interrupted()
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# reconnection: fixed delay, max 3 attempts, injectable sleep, resumption handle
# --------------------------------------------------------------------------------------------


async def test_reconnect_succeeds_before_third_attempt(fake_server):
    fake_server.plan_connection(send=[audio_chunk_message(b"before-drop"), DROP])
    fake_server.plan_connection(refuse=True)  # reconnect attempt 1: refused
    fake_server.plan_connection()  # reconnect attempt 2: accepted

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    session = GeminiLiveSession(ws_url=fake_server.ws_url, sleep=fake_sleep)
    await session.connect(system_instruction="x", voice="Kore")

    try:
        audio_iter = session.audio_out()
        assert await _anext(audio_iter) == b"before-drop"

        # Reconnection happens in the background; wait for it to finish (fast -- sleep is faked).
        await wait_until(lambda: len(fake_server.connections) == 3, timeout=2.0)
        assert sleep_calls == [1.0, 1.0]

        # The session is usable again: audio keeps flowing.
        await session.send_audio(b"after-reconnect")
        await wait_until(lambda: len(fake_server.connections[2].received) == 1, timeout=2.0)
        sent = fake_server.connections[2].received[0]["realtimeInput"]["audio"]
        assert base64.b64decode(sent["data"]) == b"after-reconnect"

        # No SessionLost was raised.
        with pytest.raises(TimeoutError):
            await _anext(session.events(), timeout=0.2)
    finally:
        await session.close()


async def test_reconnect_gives_up_after_three_attempts(fake_server):
    fake_server.plan_connection(send=[DROP])
    fake_server.plan_connection(refuse=True)
    fake_server.plan_connection(refuse=True)
    fake_server.plan_connection(refuse=True)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    session = GeminiLiveSession(ws_url=fake_server.ws_url, sleep=fake_sleep)
    await session.connect(system_instruction="x", voice="Kore")

    try:
        event = await _anext(session.events(), timeout=2.0)
        assert isinstance(event, SessionLost)

        # Exactly one SessionLost, no more.
        with pytest.raises(TimeoutError):
            await _anext(session.events(), timeout=0.2)

        # Initial connection + exactly 3 reconnect attempts, no 4th.
        assert len(fake_server.connections) == 4
        assert sleep_calls == [1.0, 1.0, 1.0]
    finally:
        await session.close()


async def test_reconnect_delay_is_injectable_and_takes_no_real_time(fake_server):
    """The acceptance criterion requires all 3 attempts (with their delays) to complete well
    inside 5 real seconds in the default configuration. With a no-op injected sleep this test
    asserts the whole give-up path takes nowhere near even 1 second of real wall-clock time --
    proving `sleep` is actually being used instead of a real `asyncio.sleep`."""
    fake_server.plan_connection(send=[DROP])
    fake_server.plan_connection(refuse=True)
    fake_server.plan_connection(refuse=True)
    fake_server.plan_connection(refuse=True)

    async def no_delay(_seconds: float) -> None:
        return None

    session = GeminiLiveSession(ws_url=fake_server.ws_url, sleep=no_delay)
    await session.connect(system_instruction="x", voice="Kore")
    try:
        start = asyncio.get_event_loop().time()
        await _anext(session.events(), timeout=2.0)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 1.0, elapsed
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# tool declarations / tool_call / send_tool_response round trip
# --------------------------------------------------------------------------------------------


async def test_tool_call_round_trip(fake_server):
    responses: list[dict] = []

    def on_message(msg: dict) -> None:
        responses.append(msg)
        return None

    fake_server.plan_connection(
        send=[tool_call_message("call-1", "get_ticket", {"key": "OPS-1"})],
        on_message=on_message,
    )
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore", tools=TOOLS)

    try:
        event = await _anext(session.events())
        assert event == ToolCall(id="call-1", name="get_ticket", args={"key": "OPS-1"})

        await session.send_tool_response("call-1", {"status": "Done", "summary": "Fixed"})
        await wait_until(lambda: len(responses) == 1)

        sent = responses[0]["toolResponse"]["functionResponses"][0]
        assert sent["id"] == "call-1"
        assert sent["response"] == {"status": "Done", "summary": "Fixed"}
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# say(): request shape + transcript capture
# --------------------------------------------------------------------------------------------


async def test_say_sends_correct_request_and_captures_transcript(fake_server):
    """Mirrors the real API's protocol: a transcript chunk with no `finished` field, followed by
    separate `generationComplete`/`turnComplete` messages -- see the regression test below for
    the full story of the bug this shape would have caught."""
    script = "Standup is cancelled today."
    captured: list[dict] = []

    def on_message(msg: dict):
        captured.append(msg)
        return [
            transcript_message(script, output=True),
            GENERATION_COMPLETE_MESSAGE,
            TURN_COMPLETE_MESSAGE,
        ]

    fake_server.plan_connection(on_message=on_message)
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore")

    try:
        await session.say(script)
        await wait_until(lambda: len(captured) == 1)

        content = captured[0]["clientContent"]
        assert content["turnComplete"] is True
        text = content["turns"][0]["parts"][0]["text"]
        assert script in text  # exact wording is an implementation judgment call; script
        # appearing verbatim in the instruction is what matters here.

        # One non-final running-transcript update for the chunk, then the final accumulated one.
        events_iter = session.events()
        assert await _anext(events_iter) == TranscriptUpdate(text=script, is_final=False)
        assert await _anext(events_iter) == TranscriptUpdate(text=script, is_final=True)
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# Regression (#11 follow-up): real-API turn-completion protocol bug
#
# Confirmed against real wire captures: `outputTranscription`/`inputTranscription` chunks never
# carry a `finished` field. Turn completion is a later, separate `serverContent` message with no
# transcription payload (`generationComplete` then `turnComplete`). The old fake server/tests
# assumed `finished` was real and settable, which hid this: this test reproduces the exact
# captured shape and would have caught the bug (no `TranscriptUpdate(is_final=True)` would ever
# have been emitted under the old, buggy `_handle_server_content`).
# --------------------------------------------------------------------------------------------


async def test_output_transcript_accumulates_until_turn_complete(fake_server):
    fake_server.plan_connection(
        send=[
            EMPTY_SERVER_CONTENT_MESSAGE,
            EMPTY_SERVER_CONTENT_MESSAGE,
            audio_chunk_message(b"audio-1"),
            transcript_message("Hello, this "),
            audio_chunk_message(b"audio-2"),
            transcript_message("a connectivity "),
            transcript_message("test."),
            audio_chunk_message(b"audio-3"),
            GENERATION_COMPLETE_MESSAGE,
            TURN_COMPLETE_MESSAGE,
        ]
    )
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore")

    try:
        events_iter = session.events()

        # One non-final event per chunk, each carrying the *running* accumulated text so far.
        assert await _anext(events_iter) == TranscriptUpdate(text="Hello, this ", is_final=False)
        assert await _anext(events_iter) == TranscriptUpdate(
            text="Hello, this a connectivity ", is_final=False
        )
        assert await _anext(events_iter) == TranscriptUpdate(
            text="Hello, this a connectivity test.", is_final=False
        )

        # Exactly one final event, with the full concatenated text, emitted at generationComplete
        # (the first of the two completion signals to arrive).
        assert await _anext(events_iter) == TranscriptUpdate(
            text="Hello, this a connectivity test.", is_final=True
        )

        # turnComplete arriving afterwards (for the same turn) produces no second final event --
        # the buffer was already reset.
        with pytest.raises(TimeoutError):
            await _anext(events_iter, timeout=0.2)

        # Pure audio chunks and the empty serverContent messages never produced a transcript
        # event; only real transcription chunks and completion signals did.
        audio_iter = session.audio_out()
        assert await _anext(audio_iter) == b"audio-1"
        assert await _anext(audio_iter) == b"audio-2"
        assert await _anext(audio_iter) == b"audio-3"
    finally:
        await session.close()


# --------------------------------------------------------------------------------------------
# GOOGLE_API_KEY: never logged, never in an exception, never in repr()
# --------------------------------------------------------------------------------------------


async def test_api_key_never_appears_in_logs_exceptions_or_repr(monkeypatch, caplog, fake_server):
    monkeypatch.setenv("GOOGLE_API_KEY", FAKE_API_KEY)
    caplog.set_level(logging.DEBUG)

    # Normal connect + send, then a forced failure path: drop (deterministically, right after
    # receiving the client's send_audio message, so there's no race against the drop), then
    # exhaust all 3 reconnect attempts so a SessionLost is produced.
    fake_server.plan_connection(send=[audio_chunk_message(b"chunk")], on_message=lambda _msg: DROP)
    fake_server.plan_connection(refuse=True)
    fake_server.plan_connection(refuse=True)
    fake_server.plan_connection(refuse=True)

    async def fake_sleep(_seconds: float) -> None:
        return None

    session = GeminiLiveSession(ws_url=fake_server.ws_url, sleep=fake_sleep)
    await session.connect(system_instruction="x", voice="Kore")
    assert await _anext(session.audio_out()) == b"chunk"
    await session.send_audio(b"abc")
    event = await _anext(session.events(), timeout=2.0)
    assert isinstance(event, SessionLost)

    await session.close()
    await session.close()  # idempotent, exercised here too

    # A separate forced connect-time failure (nothing listening) to check exception text.
    unreachable = GeminiLiveSession(ws_url="ws://127.0.0.1:1", sleep=fake_sleep)
    raised: Exception | None = None
    try:
        await unreachable.connect(system_instruction="x", voice="Kore")
    except Exception as exc:  # noqa: BLE001 - deliberately broad: checking its text, not type
        raised = exc
    assert raised is not None
    assert FAKE_API_KEY not in str(raised)
    assert FAKE_API_KEY not in repr(raised)

    assert FAKE_API_KEY not in repr(session)
    assert FAKE_API_KEY not in repr(unreachable)
    for record in caplog.records:
        assert FAKE_API_KEY not in record.getMessage()


# --------------------------------------------------------------------------------------------
# model id precedence
# --------------------------------------------------------------------------------------------


def test_model_id_precedence(monkeypatch):
    monkeypatch.delenv("GEMINI_LIVE_MODEL", raising=False)
    assert GeminiLiveSession()._model == DEFAULT_GEMINI_LIVE_MODEL

    monkeypatch.setenv("GEMINI_LIVE_MODEL", "gemini-env-live-model")
    assert GeminiLiveSession()._model == "gemini-env-live-model"

    # Explicit param wins over the env var.
    assert GeminiLiveSession(model="gemini-explicit-model")._model == "gemini-explicit-model"


# --------------------------------------------------------------------------------------------
# close(): idempotent, ends audio_out()/events() iteration
# --------------------------------------------------------------------------------------------


async def test_close_is_idempotent_and_ends_iteration(fake_server):
    fake_server.plan_connection()
    session = GeminiLiveSession(ws_url=fake_server.ws_url)
    await session.connect(system_instruction="x", voice="Kore")

    audio_items: list[bytes] = []
    event_items: list[object] = []

    async def drain_audio():
        async for item in session.audio_out():
            audio_items.append(item)

    async def drain_events():
        async for item in session.events():
            event_items.append(item)

    audio_task = asyncio.create_task(drain_audio())
    events_task = asyncio.create_task(drain_events())
    await asyncio.sleep(0.05)  # let both tasks start waiting on their (empty) queues

    await session.close()
    await session.close()  # idempotent: does not raise

    await asyncio.wait_for(audio_task, timeout=2.0)
    await asyncio.wait_for(events_task, timeout=2.0)
    assert audio_items == []
    assert event_items == []


# --------------------------------------------------------------------------------------------
# Live: genuinely-live tests, opt-in only (skipped by default)
# --------------------------------------------------------------------------------------------


@pytest.mark.live
async def test_live_session_survives_20_minutes():
    """Holds a real session open for 20 minutes -- past the 15-minute audio cap in design.md §3
    -- and asserts it is still responsive at the end (one `say()` round trip). Skipped unless
    `GOOGLE_API_KEY` is set. Run it with:

        GOOGLE_API_KEY=... uv run pytest -m live \
            tests/test_live_session.py::test_live_session_survives_20_minutes
    """
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY is not set")

    session = GeminiLiveSession()
    await session.connect(system_instruction="You are a helpful assistant.", voice="Kore")
    try:
        await asyncio.sleep(20 * 60)
        await session.say("Still here.")
        event = await asyncio.wait_for(session.events().__anext__(), timeout=30)
        assert isinstance(event, TranscriptUpdate)
    finally:
        await session.close()


@pytest.mark.live
async def test_live_say_transcript_match_rate():
    """Runs `say()` 10 times against the real API and asserts at least 9 of the 10 output
    transcripts equal the script, ignoring case and punctuation. Reports the pass count. Skipped
    unless `GOOGLE_API_KEY` is set. Run it with:

        GOOGLE_API_KEY=... uv run pytest -m live \
            tests/test_live_session.py::test_live_say_transcript_match_rate
    """
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY is not set")

    import string

    script = "The quarterly report is ready for review."

    def normalize(text: str) -> str:
        return text.strip().lower().translate(str.maketrans("", "", string.punctuation))

    passes = 0
    for _ in range(10):
        session = GeminiLiveSession()
        await session.connect(system_instruction="You are a helpful assistant.", voice="Kore")
        try:
            await session.say(script)
            events_iter = session.events()
            transcript = ""
            while True:
                event = await asyncio.wait_for(events_iter.__anext__(), timeout=30)
                if isinstance(event, TranscriptUpdate):
                    transcript = event.text
                    if event.is_final:
                        break
            if normalize(transcript) == normalize(script):
                passes += 1
        finally:
            await session.close()

    print(f"say() transcript match rate: {passes}/10")
    assert passes >= 9, f"only {passes}/10 transcripts matched"
