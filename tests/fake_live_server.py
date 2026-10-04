"""A reusable fake Gemini Live WebSocket server for #11's tests.

Speaks the same message shapes the real Gemini Live `BidiGenerateContent` protocol does: `setup`
-> `setupComplete`, `serverContent` (audio chunks via `inlineData`, `interrupted`, input/output
transcripts), `toolCall` -> `toolResponse`, and `sessionResumptionUpdate` -- per
`_docs/testing-guidelines.md`'s Gemini Live row. Built once here and reused by every test in
`tests/test_live_session.py` (interruption, reconnection, tool-calling, say()), per that same
guideline ("Build it once ... and reuse it across the interruption, reconnection, and
tool-calling tests rather than re-implementing it per test").

Usage
-----
```python
server = FakeLiveServer()
await server.start()
server.plan_connection(send=[audio_chunk_message(b"..."), INTERRUPTED_MESSAGE])
server.plan_connection(refuse=True)          # e.g. a reconnect attempt that fails
...
await server.stop()
```

Each call to `plan_connection` queues the behaviour for the *next* accepted TCP connection --
one per `connect()` call or reconnect attempt. If a connection arrives with no plan queued, it is
accepted, sent `setupComplete`, and then just listens (no scripted sends) -- this is the common
case for a connection that is supposed to just "stay up".
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

# Sentinel placed in a `send` list to make the fake server close the connection immediately at
# that point, simulating an unexpected drop.
DROP = object()

INTERRUPTED_MESSAGE: dict[str, Any] = {"serverContent": {"interrupted": True}}


def audio_chunk_message(data: bytes, *, rate: int = 24000) -> dict[str, Any]:
    """A `serverContent` message carrying one `inlineData` audio chunk (base64-encoded on the
    wire, as the real protocol does)."""
    return {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": f"audio/pcm;rate={rate}",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    }
                ]
            }
        }
    }


def tool_call_message(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"toolCall": {"functionCalls": [{"id": call_id, "name": name, "args": args}]}}


def transcript_message(text: str, *, finished: bool = True, output: bool = True) -> dict[str, Any]:
    key = "outputTranscription" if output else "inputTranscription"
    return {"serverContent": {key: {"text": text, "finished": finished}}}


def resumption_update_message(handle: str) -> dict[str, Any]:
    return {"sessionResumptionUpdate": {"newHandle": handle, "resumable": True}}


@dataclass
class ConnectionRecord:
    """What actually happened on one accepted (or refused) connection."""

    refused: bool = False
    setup: dict[str, Any] | None = None
    received: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _ConnectionPlan:
    refuse: bool = False
    send: list[Any] = field(default_factory=list)
    on_message: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


class FakeLiveServer:
    """An in-process fake Gemini Live server. See the module docstring."""

    def __init__(self) -> None:
        self.connections: list[ConnectionRecord] = []
        self.ws_url: str | None = None
        self._plans: list[_ConnectionPlan] = []
        self._server: Any = None

    def plan_connection(
        self,
        *,
        refuse: bool = False,
        send: list[Any] | None = None,
        on_message: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        """Queue the behaviour for the next accepted connection.

        `refuse`: close immediately without completing setup (simulates a refused reconnect).
        `send`: messages (dicts) sent in order right after `setupComplete`; `DROP` in this list
            closes the connection at that point instead of sending anything further.
        `on_message`: called with each JSON message the client sends after setup; its return
            value is sent back as a reply if it's a dict (used for the tool-call round trip),
            closes the connection immediately if it's `DROP` (useful to drop deterministically
            right after a specific client message instead of racing a scripted `send`/`DROP`
            against that message), or does nothing further if it's `None`.
        """
        self._plans.append(
            _ConnectionPlan(refuse=refuse, send=list(send or []), on_message=on_message)
        )

    async def start(self) -> str:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        self.ws_url = f"ws://{host}:{port}"
        return self.ws_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws: Any) -> None:
        plan = self._plans.pop(0) if self._plans else _ConnectionPlan()
        record = ConnectionRecord(refused=plan.refuse)
        self.connections.append(record)

        if plan.refuse:
            await ws.close(code=1011, reason="fake-live-server: refused")
            return

        raw = await ws.recv()
        record.setup = json.loads(raw)["setup"]
        await ws.send(json.dumps({"setupComplete": {}}))

        for item in plan.send:
            if item is DROP:
                await ws.close(code=1011, reason="fake-live-server: dropped")
                return
            await ws.send(json.dumps(item))

        try:
            async for raw in ws:
                msg = json.loads(raw)
                record.received.append(msg)
                if plan.on_message is not None:
                    reply = plan.on_message(msg)
                    if reply is DROP:
                        await ws.close(code=1011, reason="fake-live-server: dropped")
                        return
                    if reply is not None:
                        await ws.send(json.dumps(reply))
        except ConnectionClosed:
            pass


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.01
) -> None:
    """Poll `predicate` until it is true, raising `AssertionError` if `timeout` elapses first.
    Used only to wait for the fake server to have *received* something -- never for the
    reconnect backoff itself, which tests control via an injected no-op sleep."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)
