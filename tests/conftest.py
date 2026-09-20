import pytest
from fastapi.testclient import TestClient

from rbaa.config import Settings
from rbaa.health import Probe
from rbaa.main import create_app


async def _up() -> bool:
    return True


async def _down() -> bool:
    return False


@pytest.fixture
def make_client():
    """Build a client whose dependencies are fakes; no network, no containers."""

    def _make(postgres_up: bool = True, redis_up: bool = True) -> TestClient:
        probes: dict[str, Probe] = {
            "postgres": _up if postgres_up else _down,
            "redis": _up if redis_up else _down,
        }
        return TestClient(create_app(Settings(_env_file=None), probes=probes))

    return _make
