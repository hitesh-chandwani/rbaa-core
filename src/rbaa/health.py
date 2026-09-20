"""Dependency probes used by the `/ready` endpoint.

Each probe does a real round trip and returns True when the dependency answered, False for any
failure or timeout. Exception details are deliberately swallowed so nothing leaks to callers.
"""

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg
import redis.asyncio as aioredis

PROBE_TIMEOUT_SECONDS = 3.0

Probe = Callable[[], Awaitable[bool]]


async def _postgres_round_trip(database_url: str) -> bool:
    conn = await asyncpg.connect(database_url, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        return await conn.fetchval("SELECT 1") == 1
    finally:
        await conn.close()


async def _redis_round_trip(redis_url: str) -> bool:
    client = aioredis.from_url(
        redis_url,
        socket_connect_timeout=PROBE_TIMEOUT_SECONDS,
        socket_timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def _guarded(coro: Awaitable[bool]) -> bool:
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_SECONDS + 1)
    except Exception:
        return False


def postgres_probe(database_url: str) -> Probe:
    return lambda: _guarded(_postgres_round_trip(database_url))


def redis_probe(redis_url: str) -> Probe:
    return lambda: _guarded(_redis_round_trip(redis_url))


async def unavailable_dependencies(probes: dict[str, Probe]) -> list[str]:
    """Run all probes concurrently; return the names of the failed ones, in probe order."""
    names = list(probes)
    results = await asyncio.gather(*(probes[name]() for name in names))
    return [name for name, ok in zip(names, results, strict=True) if not ok]
