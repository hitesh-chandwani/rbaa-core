import re
from pathlib import Path

from fastapi.testclient import TestClient

from rbaa.config import Settings
from rbaa.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def _example_keys() -> set[str]:
    keys = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_env_example_covers_every_setting_the_app_reads():
    read_by_app = {name.upper() for name in Settings.model_fields}
    assert read_by_app <= _example_keys()


def test_env_example_covers_every_variable_compose_reads():
    compose = (ROOT / "docker-compose.yml").read_text()
    read_by_compose = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose))
    assert read_by_compose <= _example_keys()


def test_env_example_google_api_key_is_empty():
    line = next(
        line
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line.startswith("GOOGLE_API_KEY=")
    )
    assert line == "GOOGLE_API_KEY="


def test_app_starts_with_empty_google_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    assert TestClient(create_app(Settings(_env_file=None))).get("/health").status_code == 200
