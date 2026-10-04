"""Tests for `rbaa.integrations.jira.fetch_jira_activity` (#8).

Every test is offline: HTTP is mocked at the client transport with `httpx2.MockTransport` (see
`_docs/testing-guidelines.md`, "Tooling to add" - `respx` does not work with `httpx2`). Recorded
responses are stored as JSON envelopes (`{"status": ..., "headers": ..., "body": ...}`) in
`tests/fixtures/http/jira/`, mirroring `tests/test_github_fetcher.py` (#7). The wall clock is
never read live: rate-limit tests monkeypatch `rbaa.integrations.jira._now`/`_sleep`, the two
seams the module exposes for this (there is no clock parameter on the public signature, which #8
pins exactly, mirroring #7).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from rbaa.agents import Agent
from rbaa.integrations import jira
from rbaa.integrations.jira import (
    JiraActivity,
    JiraBlocker,
    JiraFetchError,
    JiraStatusChange,
    JiraTicket,
    RateLimitError,
    fetch_jira_activity,
)
from rbaa.roles import Role
from rbaa.security import PermissionDenied

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "jira"
SINCE = datetime(2024, 1, 1, tzinfo=UTC)
SECRET_TOKEN = "SECRET_JIRA_TOKEN_123"

ROLES: dict[str, Role] = {
    "full_access": Role(
        name="full_access",
        title="Full Access",
        responsibilities=["Do the work"],
        communication_style="Plain",
        standup_max_seconds=60,
        default_voice="Kore",
        tool_permissions=["jira:read"],
        guidelines="Guidelines.",
    ),
}


def _agent(
    agent_id: str = "bot",
    *,
    account_id: str = "bot-acc",
    allowed_jira_projects: list[str] | None = None,
    jira_token: str = "tok-fake-jira",
) -> Agent:
    return Agent(
        agent_id=agent_id,
        role="full_access",
        display_name=agent_id,
        github_username=f"{agent_id}-gh",
        jira_account_id=account_id,
        github_token_env=f"{agent_id.upper()}_GH",
        jira_token_env=f"{agent_id.upper()}_JIRA",
        allowed_repos=[],
        allowed_jira_projects=(
            allowed_jira_projects if allowed_jira_projects is not None else ["PROJ"]
        ),
        github_token=SecretStr("tok-fake-github"),
        jira_token=SecretStr(jira_token),
    )


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _response(fixture: dict, *, extra_headers: dict[str, str] | None = None) -> httpx2.Response:
    headers = dict(fixture.get("headers", {}))
    if extra_headers:
        headers.update(extra_headers)
    return httpx2.Response(fixture["status"], headers=headers, json=fixture["body"])


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _search_path() -> str:
    return "/rest/api/3/search"


def _single_issue_handler(calls: list[str] | None = None):
    """Serves one plain ticket for project PROJ, nothing else."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == _search_path():
            return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


# --- shape of the result ---


async def test_fetch_returns_ticket_with_all_required_fields():
    agent = _agent()
    client = _client(_single_issue_handler())

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert result == JiraActivity(
        tickets=[
            JiraTicket(
                key="PROJ-1",
                summary="Fix the widget renderer",
                status="In Progress",
                status_category="indeterminate",
                updated=datetime(2024, 2, 1, 10, 0, tzinfo=UTC),
                url=f"{jira.JIRA_API_BASE}/browse/PROJ-1",
            )
        ],
        status_changes=[],
        completed=[],
        blockers=[],
        errors=[],
    )


# --- documented endpoint shape ---


async def test_search_request_uses_documented_query_parameters():
    calls: list[str] = []
    agent = _agent(account_id="acc-123", allowed_jira_projects=["PROJ"])
    client = _client(_single_issue_handler(calls))

    await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    search_calls = [c for c in calls if _search_path() in c]
    assert len(search_calls) == 1
    url = httpx2.URL(search_calls[0])
    assert url.params["jql"] == "project = PROJ AND assignee = acc-123 ORDER BY updated DESC"
    assert url.params["expand"] == "changelog"
    assert url.params["fields"] == "summary,status,updated,labels,issuelinks"
    assert url.params["startAt"] == "0"
    assert url.params["maxResults"] == "100"


async def test_search_url_is_against_jira_api_base():
    calls: list[str] = []
    agent = _agent()
    client = _client(_single_issue_handler(calls))

    await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert calls[0].startswith(f"{jira.JIRA_API_BASE}{_search_path()}")


# --- status changes ---


async def test_status_change_records_from_to_and_changed_at():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_status_change.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert result.status_changes == [
        JiraStatusChange(
            key="PROJ-2",
            from_status="To Do",
            to_status="In Progress",
            changed_at=datetime(2024, 2, 5, 12, 0, tzinfo=UTC),
        )
    ]


async def test_status_change_history_before_since_is_excluded():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_status_change.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    # The fixture has a second history (Backlog -> To Do) dated before `since`; only the
    # at/after-`since` entry (To Do -> In Progress) should appear.
    froms = {c.from_status for c in result.status_changes}
    assert "Backlog" not in froms
    assert len(result.status_changes) == 1


async def test_status_change_not_reaching_done_is_not_completed():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_status_change.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert result.completed == []


# --- moved to Done ---


async def test_ticket_moved_to_done_at_or_after_since_is_completed():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_moved_to_done.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert [t.key for t in result.completed] == ["PROJ-3"]
    assert result.completed[0].status_category == "done"
    assert [c.to_status for c in result.status_changes] == ["Done"]


async def test_ticket_in_tickets_list_even_when_completed():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_moved_to_done.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert [t.key for t in result.tickets] == ["PROJ-3"]


# --- blockers ---


async def test_blocker_detected_via_blocked_label():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_blockers.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert JiraBlocker(key="PROJ-4", blocking_key=None) in result.blockers


async def test_blocker_detected_via_unresolved_issuelink():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_blockers.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert JiraBlocker(key="PROJ-5", blocking_key="PROJ-99") in result.blockers


async def test_resolved_issuelink_is_not_a_blocker():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_blockers.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent()
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert not any(b.key == "PROJ-6" for b in result.blockers)
    assert len(result.blockers) == 2  # only PROJ-4 and PROJ-5


# --- pagination ---


async def test_pagination_follows_start_at_max_results_total_for_120_issues():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            start_at = request.url.params.get("startAt")
            if start_at == "0":
                return _response(_fixture("search_page1.json"))
            if start_at == "100":
                return _response(_fixture("search_page2.json"))
            raise AssertionError(f"unexpected startAt: {start_at}")
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PAGE"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert len(result.tickets) == 120
    keys = {t.key for t in result.tickets}
    assert len(keys) == 120  # no duplicates
    assert "PAGE-0" in keys
    assert "PAGE-119" in keys


# --- rate limiting ---


async def test_rate_limit_retry_after_then_success(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(jira, "_sleep", fake_sleep)

    attempts = {"search": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            attempts["search"] += 1
            if attempts["search"] == 1:
                return _response(_fixture("rate_limit_retry_after.json"))
            return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PROJ"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client, max_retries=3)

    assert sleep_calls == [1.0]  # the fixture's Retry-After value
    assert attempts["search"] == 2
    assert result.errors == []
    assert [t.key for t in result.tickets] == ["PROJ-1"]


async def test_rate_limit_exhausted_raises_rate_limit_error_with_reset_at(monkeypatch):
    fixed_now = datetime(2024, 3, 1, 0, 0, 0, tzinfo=UTC)
    expected_reset_at = fixed_now + timedelta(seconds=300)

    monkeypatch.setattr(jira, "_now", lambda: fixed_now)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(jira, "_sleep", fake_sleep)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("rate_limit_exhausted.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PROJ"])
    client = _client(handler)

    with pytest.raises(RateLimitError) as exc_info:
        await fetch_jira_activity(agent, ROLES, SINCE, client=client, max_retries=2)

    assert exc_info.value.reset_at == expected_reset_at
    assert sleep_calls == [300.0]  # one wait between the two attempts, none after the last


# --- non-429 error isolation ---


async def test_404_on_one_project_does_not_stop_others():
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        project = request.url.params.get("jql", "")
        if request.url.path == _search_path():
            if "BROKEN" in project:
                return _response(_fixture("project_not_found.json"))
            if "PROJ" in project:
                return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["BROKEN", "PROJ"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    expected_error = JiraFetchError(
        project="BROKEN",
        status=404,
        message="The project is not found or you don't have permission to see it.",
    )
    assert result.errors == [expected_error]
    assert [t.key for t in result.tickets] == ["PROJ-1"]  # PROJ still fetched


async def test_401_on_one_project_does_not_stop_others():
    def handler(request: httpx2.Request) -> httpx2.Response:
        project = request.url.params.get("jql", "")
        if request.url.path == _search_path():
            if "NOAUTH" in project:
                return _response(_fixture("project_unauthorized.json"))
            if "PROJ" in project:
                return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["NOAUTH", "PROJ"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert len(result.errors) == 1
    assert result.errors[0].project == "NOAUTH"
    assert result.errors[0].status == 401
    assert [t.key for t in result.tickets] == ["PROJ-1"]


# --- no tickets ---


async def test_no_tickets_returns_all_empty_lists_and_no_errors():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("search_empty.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PROJ"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert result == JiraActivity()


# --- permission gate ---


async def test_permission_denied_for_one_project_is_caught_and_others_continue(monkeypatch):
    real_require_permission = jira.require_permission

    def fake_require_permission(agent, action, resource, roles):
        if resource == "DENIED":
            raise PermissionDenied(agent.agent_id, action, resource, "denied for this test")
        return real_require_permission(agent, action, resource, roles)

    monkeypatch.setattr(jira, "require_permission", fake_require_permission)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if "DENIED" in str(request.url):
            raise AssertionError("no request should ever reach the denied project")
        if request.url.path == _search_path():
            return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["DENIED", "PROJ"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert len(result.errors) == 1
    assert result.errors[0].project == "DENIED"
    assert result.errors[0].status == 403
    assert [t.key for t in result.tickets] == ["PROJ-1"]


async def test_require_permission_called_before_any_request_for_each_project(monkeypatch):
    real_require_permission = jira.require_permission
    timeline: list[tuple[str, str]] = []

    def spy_require_permission(agent, action, resource, roles):
        timeline.append(("permission", resource))
        return real_require_permission(agent, action, resource, roles)

    monkeypatch.setattr(jira, "require_permission", spy_require_permission)

    def handler(request: httpx2.Request) -> httpx2.Response:
        timeline.append(("request", str(request.url)))
        if request.url.path == _search_path():
            return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PROJ"])
    client = _client(handler)

    await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert timeline[0] == ("permission", "PROJ")
    assert all(kind == "request" for kind, _ in timeline[1:])


async def test_project_outside_allowed_jira_projects_is_never_requested():
    """`fetch_jira_activity`'s only source of projects is `agent.allowed_jira_projects`, exactly
    like #7's `allowed_repos` - so a project never in that list is never requested. Unlike #7's
    analogous test, the acceptance criterion's wording also says such a project is "added to
    errors"; that is not achievable here, since the function has no way to ever learn such a
    project exists (see the module docstring's "Judgment calls" and the engineer's issue
    comment)."""
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if "OUTSIDE" in str(request.url):
            raise AssertionError("a project outside allowed_jira_projects must never be requested")
        if request.url.path == _search_path():
            return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    # "OUTSIDE" is never in allowed_jira_projects, so fetch_jira_activity has no way to see it.
    agent = _agent(allowed_jira_projects=["PROJ"])
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert not any("OUTSIDE" in c for c in calls)
    assert result.errors == []


# --- default client, timeout, isolation ---


async def test_default_client_has_a_configured_timeout():
    client = jira._default_client()
    try:
        assert client.timeout.connect == 30.0
        assert client.timeout.read == 30.0
    finally:
        await client.aclose()


async def test_fetch_builds_and_closes_default_client_when_none_given(monkeypatch):
    built_client = _client(_single_issue_handler())
    monkeypatch.setattr(jira, "_default_client", lambda: built_client)

    agent = _agent(allowed_jira_projects=["PROJ"])
    assert not built_client.is_closed

    result = await fetch_jira_activity(agent, ROLES, SINCE)

    assert [t.key for t in result.tickets] == ["PROJ-1"]
    assert built_client.is_closed  # fetch_jira_activity owns and closes the client it built


async def test_caller_supplied_client_is_not_closed_by_fetch():
    client = _client(_single_issue_handler())
    agent = _agent(allowed_jira_projects=["PROJ"])

    await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert not client.is_closed  # the caller owns this client


# --- async, with an explicit timeout (NFR, testing-guidelines.md rule 8) ---


async def test_fetch_completes_within_timeout():
    agent = _agent(allowed_jira_projects=["PROJ"])
    client = _client(_single_issue_handler())

    result = await asyncio.wait_for(
        fetch_jira_activity(agent, ROLES, SINCE, client=client), timeout=5.0
    )

    assert [t.key for t in result.tickets] == ["PROJ-1"]


# --- credentials ---


async def test_jira_token_is_sent_as_bearer_header():
    captured_headers: list[httpx2.Headers] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured_headers.append(request.headers)
        if request.url.path == _search_path():
            return _response(_fixture("search_single.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PROJ"], jira_token=SECRET_TOKEN)
    client = _client(handler)

    await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert captured_headers
    assert all(h["authorization"] == f"Bearer {SECRET_TOKEN}" for h in captured_headers)


async def test_jira_token_never_appears_in_fetch_error_or_result_repr():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _search_path():
            return _response(_fixture("project_not_found.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_jira_projects=["PROJ"], jira_token=SECRET_TOKEN)
    client = _client(handler)

    result = await fetch_jira_activity(agent, ROLES, SINCE, client=client)

    assert result.errors[0].status == 404
    assert SECRET_TOKEN not in repr(result)
    assert SECRET_TOKEN not in str(result)
    assert SECRET_TOKEN not in result.errors[0].message
