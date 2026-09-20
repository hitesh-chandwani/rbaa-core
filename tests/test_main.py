import pytest


def test_health_returns_ok(make_client):
    response = make_client().get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_does_not_depend_on_dependencies(make_client):
    response = make_client(postgres_up=False, redis_up=False).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_with_healthy_dependencies(make_client):
    response = make_client().get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize(
    ("postgres_up", "redis_up", "expected"),
    [
        (False, True, ["postgres"]),
        (True, False, ["redis"]),
        (False, False, ["postgres", "redis"]),
    ],
)
def test_ready_503_names_unavailable_dependencies(make_client, postgres_up, redis_up, expected):
    response = make_client(postgres_up=postgres_up, redis_up=redis_up).get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "unavailable": expected}
