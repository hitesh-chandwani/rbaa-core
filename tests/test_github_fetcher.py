"""Tests for `rbaa.integrations.github.fetch_github_activity` (#7).

Every test is offline: HTTP is mocked at the client transport with `httpx2.MockTransport` (see
`_docs/testing-guidelines.md`, "Tooling to add" - `respx` does not work with `httpx2`). Recorded
responses are stored as JSON envelopes (`{"status": ..., "headers": ..., "body": ...}`) in
`tests/fixtures/http/github/`. The wall clock is never read live: rate-limit tests monkeypatch
`rbaa.integrations.github._now`/`_sleep`, the two seams the module exposes for this (there is no
clock parameter on the public signature, which #7 pins exactly).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from rbaa.agents import Agent
from rbaa.integrations import github
from rbaa.integrations.github import (
    CommitActivity,
    GitHubActivity,
    GitHubFetchError,
    PullRequestActivity,
    RateLimitError,
    fetch_github_activity,
)
from rbaa.roles import Role
from rbaa.security import PermissionDenied

FIXTURES = Path(__file__).parent / "fixtures" / "http" / "github"
SINCE = datetime(2024, 1, 1, tzinfo=UTC)
SECRET_TOKEN = "SECRET_GH_TOKEN_123"

ROLES: dict[str, Role] = {
    "full_access": Role(
        name="full_access",
        title="Full Access",
        responsibilities=["Do the work"],
        communication_style="Plain",
        standup_max_seconds=60,
        default_voice="Kore",
        tool_permissions=["github:read"],
        guidelines="Guidelines.",
    ),
}


def _agent(
    agent_id: str = "bot",
    *,
    username: str = "octobot",
    allowed_repos: list[str] | None = None,
    github_token: str = "tok-fake-github",
) -> Agent:
    return Agent(
        agent_id=agent_id,
        role="full_access",
        display_name=agent_id,
        github_username=username,
        jira_account_id=f"{agent_id}-acc",
        github_token_env=f"{agent_id.upper()}_GH",
        jira_token_env=f"{agent_id.upper()}_JIRA",
        allowed_repos=allowed_repos if allowed_repos is not None else ["acme/ok"],
        allowed_jira_projects=[],
        github_token=SecretStr(github_token),
        jira_token=SecretStr("tok-fake-jira"),
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


def _search_fixture_for(query: str) -> dict:
    """The recorded search fixture matching one of the three documented `q` shapes."""
    if "review-requested" in query:
        return _fixture("search_review_single.json")
    if "is:merged" in query:
        return _fixture("search_merged_single.json")
    if "is:open" in query:
        return _fixture("search_open_single.json")
    raise AssertionError(f"unrecognised search query: {query!r}")


def _ok_repo_handler(calls: list[str] | None = None):
    """Serves one commit and one merged/open/review-requested PR for `acme/ok`, nothing else."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if calls is not None:
            calls.append(str(request.url))
        path = request.url.path
        if path == "/repos/acme/ok/commits":
            return _response(_fixture("commits_single.json"))
        if path == "/search/issues":
            return _response(_search_fixture_for(request.url.params.get("q", "")))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


# --- the four lists, and their fields ---


async def test_fetch_returns_all_four_lists_with_expected_fields():
    agent = _agent(allowed_repos=["acme/ok"])
    client = _client(_ok_repo_handler())

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert result == GitHubActivity(
        commits=[
            CommitActivity(
                id="aaaa0001",
                title="Fix the widget renderer",
                url="https://github.com/acme/ok/commit/aaaa0001",
                timestamp=datetime(2024, 2, 1, 10, 0, tzinfo=UTC),
            )
        ],
        merged_prs=[
            PullRequestActivity(
                id="acme/ok#42",
                title="Merged fix for the widget renderer",
                url="https://github.com/acme/ok/pull/42",
                timestamp=datetime(2024, 2, 5, 12, 0, tzinfo=UTC),
            )
        ],
        open_prs=[
            PullRequestActivity(
                id="acme/ok#43",
                title="Still-open improvement",
                url="https://github.com/acme/ok/pull/43",
                timestamp=datetime(2024, 2, 6, 9, 30, tzinfo=UTC),
            )
        ],
        review_requests=[
            PullRequestActivity(
                id="acme/ok#44",
                title="Please review this refactor",
                url="https://github.com/acme/ok/pull/44",
                timestamp=datetime(2024, 2, 7, 15, 45, tzinfo=UTC),
            )
        ],
        errors=[],
    )


async def test_identifiers_are_commit_sha_and_owner_repo_hash_number():
    agent = _agent(allowed_repos=["acme/ok"])
    client = _client(_ok_repo_handler())

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert result.commits[0].id == "aaaa0001"  # the raw commit sha
    assert result.merged_prs[0].id == "acme/ok#42"
    assert result.open_prs[0].id == "acme/ok#43"
    assert result.review_requests[0].id == "acme/ok#44"


# --- documented endpoint shapes ---


async def test_commits_request_uses_documented_query_parameters():
    agent = _agent(username="octobot", allowed_repos=["acme/ok"])
    calls: list[str] = []
    client = _client(_ok_repo_handler(calls))

    await fetch_github_activity(agent, ROLES, SINCE, client=client)

    commit_calls = [c for c in calls if "/repos/acme/ok/commits" in c]
    assert len(commit_calls) == 1
    url = httpx2.URL(commit_calls[0])
    assert url.params["author"] == "octobot"
    assert url.params["since"] == SINCE.isoformat()
    assert url.params["per_page"] == "100"
    assert url.params["page"] == "1"


async def test_search_queries_use_documented_q_syntax():
    agent = _agent(username="octobot", allowed_repos=["acme/ok"])
    calls: list[str] = []
    client = _client(_ok_repo_handler(calls))

    await fetch_github_activity(agent, ROLES, SINCE, client=client)

    search_qs = {httpx2.URL(c).params["q"] for c in calls if "/search/issues" in c}
    assert search_qs == {
        "repo:acme/ok type:pr author:octobot is:merged",
        "repo:acme/ok type:pr author:octobot is:open",
        "repo:acme/ok review-requested:octobot is:open",
    }


# --- since-filtering, merge-commit exclusion, foreign-author exclusion ---


def _mixed_commits_handler():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/mixed/commits":
            return _response(_fixture("commits_mixed.json"))
        if request.url.path == "/search/issues":
            return _response(_fixture("search_empty.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


async def test_since_filter_excludes_commits_before_since():
    agent = _agent(username="octobot", allowed_repos=["acme/mixed"])
    client = _client(_mixed_commits_handler())

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    ids = {c.id for c in result.commits}
    assert "mix-normal" in ids
    assert "mix-stale" not in ids  # dated before `since`


async def test_merge_commits_are_excluded_via_parents_count():
    agent = _agent(username="octobot", allowed_repos=["acme/mixed"])
    client = _client(_mixed_commits_handler())

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    ids = {c.id for c in result.commits}
    assert "mix-merge" not in ids  # has two parents


async def test_foreign_author_commits_are_excluded():
    agent = _agent(username="octobot", allowed_repos=["acme/mixed"])
    client = _client(_mixed_commits_handler())

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    ids = {c.id for c in result.commits}
    assert "mix-foreign" not in ids  # authored by someone else


async def test_pr_since_filter_uses_updated_at():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/mixed/commits":
            return _response(_fixture("commits_empty.json"))
        if request.url.path == "/search/issues":
            q = request.url.params.get("q", "")
            if "is:merged" in q:
                return _response(_fixture("search_merged_mixed.json"))
            return _response(_fixture("search_empty.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(username="octobot", allowed_repos=["acme/mixed"])
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    ids = {pr.id for pr in result.merged_prs}
    assert ids == {"acme/mixed#50"}  # #51's updated_at is before `since`


# --- pagination ---


async def test_pagination_follows_link_header_for_250_commits():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/paginated/commits":
            page = request.url.params.get("page")
            return _response(_fixture(f"commits_page{page}.json"))
        if request.url.path == "/search/issues":
            return _response(_fixture("search_empty.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(username="paginated-bot", allowed_repos=["acme/paginated"])
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert len(result.commits) == 250
    ids = {c.id for c in result.commits}
    assert len(ids) == 250  # no duplicates
    assert "page-commit-000" in ids
    assert "page-commit-249" in ids


# --- rate-limit handling ---


async def test_rate_limit_retry_after_header_then_success(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(github, "_sleep", fake_sleep)

    attempts = {"commits": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/flaky/commits":
            attempts["commits"] += 1
            if attempts["commits"] == 1:
                return _response(_fixture("rate_limit_retry_after.json"))
            return _response(_fixture("commits_single.json"))
        if request.url.path == "/search/issues":
            return _response(_fixture("search_empty.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/flaky"])
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client, max_retries=3)

    assert sleep_calls == [1.0]  # the fixture's Retry-After value
    assert attempts["commits"] == 2
    assert result.errors == []
    assert [c.id for c in result.commits] == ["aaaa0001"]


async def test_rate_limit_exhausted_raises_rate_limit_error_with_reset_at(monkeypatch):
    fixed_now = datetime(2024, 3, 1, 0, 0, 0, tzinfo=UTC)
    expected_reset_at = fixed_now + timedelta(seconds=300)

    monkeypatch.setattr(github, "_now", lambda: fixed_now)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(github, "_sleep", fake_sleep)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/limited/commits":
            fixture = _fixture("rate_limit_reset_body.json")
            reset_epoch = str(int(expected_reset_at.timestamp()))
            return _response(fixture, extra_headers={"X-RateLimit-Reset": reset_epoch})
        raise AssertionError(f"unexpected request: {request.url}")  # search must never be reached

    agent = _agent(allowed_repos=["acme/limited"])
    client = _client(handler)

    with pytest.raises(RateLimitError) as exc_info:
        await fetch_github_activity(agent, ROLES, SINCE, client=client, max_retries=2)

    assert exc_info.value.reset_at == expected_reset_at
    assert sleep_calls == [300.0]  # one wait between the two attempts, none after the last


# --- 404 on one repo ---


async def test_404_on_one_repo_does_not_stop_others():
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if request.url.path == "/repos/acme/broken/commits":
            return _response(_fixture("repo_not_found.json"))
        if request.url.path == "/repos/acme/ok/commits":
            return _response(_fixture("commits_single.json"))
        if request.url.path == "/search/issues":
            return _response(_search_fixture_for(request.url.params.get("q", "")))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/broken", "acme/ok"])
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    expected_error = GitHubFetchError(repo="acme/broken", status=404, message="Not Found")
    assert result.errors == [expected_error]
    assert [c.id for c in result.commits] == ["aaaa0001"]  # acme/ok still fetched
    # No search call should ever have reached the broken repo (404 on commits aborts that repo).
    assert not any("acme/broken" in c and "/search/issues" in c for c in calls)


# --- no activity ---


async def test_no_activity_returns_all_empty_lists_and_no_errors():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/quiet/commits":
            return _response(_fixture("commits_empty.json"))
        if request.url.path == "/search/issues":
            return _response(_fixture("search_empty.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/quiet"])
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert result == GitHubActivity()


# --- permission gate ---


async def test_permission_denied_for_one_repo_is_caught_and_others_continue(monkeypatch):
    real_require_permission = github.require_permission

    def fake_require_permission(agent, action, resource, roles):
        if resource == "acme/denied":
            raise PermissionDenied(agent.agent_id, action, resource, "denied for this test")
        return real_require_permission(agent, action, resource, roles)

    monkeypatch.setattr(github, "require_permission", fake_require_permission)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if "acme/denied" in str(request.url):
            raise AssertionError("no request should ever reach the denied repo")
        if request.url.path == "/repos/acme/ok/commits":
            return _response(_fixture("commits_single.json"))
        if request.url.path == "/search/issues":
            return _response(_search_fixture_for(request.url.params.get("q", "")))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/denied", "acme/ok"])
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert len(result.errors) == 1
    assert result.errors[0].repo == "acme/denied"
    assert result.errors[0].status == 403
    assert [c.id for c in result.commits] == ["aaaa0001"]  # acme/ok still fetched


async def test_require_permission_called_before_any_request_for_each_repo(monkeypatch):
    real_require_permission = github.require_permission
    timeline: list[tuple[str, str]] = []

    def spy_require_permission(agent, action, resource, roles):
        timeline.append(("permission", resource))
        return real_require_permission(agent, action, resource, roles)

    monkeypatch.setattr(github, "require_permission", spy_require_permission)

    def handler(request: httpx2.Request) -> httpx2.Response:
        timeline.append(("request", str(request.url)))
        if request.url.path == "/repos/acme/ok/commits":
            return _response(_fixture("commits_single.json"))
        if request.url.path == "/search/issues":
            return _response(_search_fixture_for(request.url.params.get("q", "")))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/ok"])
    client = _client(handler)

    await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert timeline[0] == ("permission", "acme/ok")
    assert all(kind == "request" for kind, _ in timeline[1:])


async def test_repo_outside_allowed_repos_is_never_requested():
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if "acme/bad" in str(request.url):
            raise AssertionError("a repo outside allowed_repos must never be requested")
        if request.url.path == "/repos/acme/ok/commits":
            return _response(_fixture("commits_single.json"))
        if request.url.path == "/search/issues":
            return _response(_search_fixture_for(request.url.params.get("q", "")))
        raise AssertionError(f"unexpected request: {request.url}")

    # "acme/bad" is never in allowed_repos, so fetch_github_activity has no way to ever see it.
    agent = _agent(allowed_repos=["acme/ok"])
    client = _client(handler)

    await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert not any("acme/bad" in c for c in calls)


# --- default client, timeout, isolation ---


async def test_default_client_has_a_configured_timeout():
    client = github._default_client()
    try:
        assert client.timeout.connect == 30.0
        assert client.timeout.read == 30.0
    finally:
        await client.aclose()


async def test_fetch_builds_and_closes_default_client_when_none_given(monkeypatch):
    built_client = _client(_ok_repo_handler())
    monkeypatch.setattr(github, "_default_client", lambda: built_client)

    agent = _agent(allowed_repos=["acme/ok"])
    assert not built_client.is_closed

    result = await fetch_github_activity(agent, ROLES, SINCE)

    assert [c.id for c in result.commits] == ["aaaa0001"]
    assert built_client.is_closed  # fetch_github_activity owns and closes the client it built


async def test_caller_supplied_client_is_not_closed_by_fetch():
    client = _client(_ok_repo_handler())
    agent = _agent(allowed_repos=["acme/ok"])

    await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert not client.is_closed  # the caller owns this client


async def test_github_token_is_sent_as_bearer_header():
    captured_headers: list[httpx2.Headers] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured_headers.append(request.headers)
        if request.url.path == "/repos/acme/ok/commits":
            return _response(_fixture("commits_single.json"))
        if request.url.path == "/search/issues":
            return _response(_search_fixture_for(request.url.params.get("q", "")))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/ok"], github_token=SECRET_TOKEN)
    client = _client(handler)

    await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert captured_headers
    assert all(h["authorization"] == f"Bearer {SECRET_TOKEN}" for h in captured_headers)


async def test_github_token_never_appears_in_fetch_error_or_result_repr():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/repos/acme/broken/commits":
            return _response(_fixture("repo_not_found.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    agent = _agent(allowed_repos=["acme/broken"], github_token=SECRET_TOKEN)
    client = _client(handler)

    result = await fetch_github_activity(agent, ROLES, SINCE, client=client)

    assert result.errors[0].status == 404
    assert SECRET_TOKEN not in repr(result)
    assert SECRET_TOKEN not in str(result)
    assert SECRET_TOKEN not in result.errors[0].message
