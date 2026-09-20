import asyncio

from fastapi.testclient import TestClient

from rbaa.config import Settings
from rbaa.health import _guarded, postgres_probe, redis_probe, unavailable_dependencies
from rbaa.main import create_app

# Nothing listens on port 1, so connecting fails immediately; no containers or network needed.
UNREACHABLE_POSTGRES = "postgresql://user:secret@127.0.0.1:1/db"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


async def _ok() -> bool:
    return True


async def _raises() -> bool:
    raise RuntimeError("postgresql://user:secret@host/db")


def test_guarded_turns_exceptions_into_false():
    assert asyncio.run(_guarded(_raises())) is False


def test_guarded_returns_true_for_a_healthy_probe():
    assert asyncio.run(_guarded(_ok())) is True


def test_unavailable_dependencies_keeps_probe_order():
    probes = {"postgres": lambda: _guarded(_raises()), "redis": lambda: _guarded(_raises())}
    assert asyncio.run(unavailable_dependencies(probes)) == ["postgres", "redis"]


def test_real_postgres_probe_reports_unreachable_server_as_down():
    assert asyncio.run(postgres_probe(UNREACHABLE_POSTGRES)()) is False


def test_real_redis_probe_reports_unreachable_server_as_down():
    assert asyncio.run(redis_probe(UNREACHABLE_REDIS)()) is False


def test_ready_response_does_not_leak_exception_text_or_credentials():
    probes = {"postgres": lambda: _guarded(_raises()), "redis": lambda: _guarded(_ok())}
    client = TestClient(create_app(Settings(_env_file=None), probes=probes))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "unavailable": ["postgres"]}
    assert "secret" not in response.text
    assert "postgresql" not in response.text
