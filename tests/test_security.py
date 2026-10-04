"""Tests for `rbaa.security.require_permission` (#6).

Every test is offline: no real GitHub/Jira call is ever made. The one test that proves "zero
requests happen" wires a bare `httpx2.MockTransport` and asserts it recorded no calls.
"""

import logging
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from rbaa.agents import Agent, load_agent
from rbaa.roles import Role
from rbaa.security import PermissionDenied, require_permission

SECRET_TOKEN = "SECRET123"

# Two roles: one with both read permissions, one with none - tool_permissions can only ever be
# "github:read"/"jira:read" (#3/#4), so this is the only variation possible today.
ROLES: dict[str, Role] = {
    "full_access": Role(
        name="full_access",
        title="Full Access",
        responsibilities=["Do the work"],
        communication_style="Plain",
        standup_max_seconds=60,
        default_voice="Kore",
        tool_permissions=["github:read", "jira:read"],
        guidelines="Guidelines.",
    ),
    "no_access": Role(
        name="no_access",
        title="No Access",
        responsibilities=["Observe only"],
        communication_style="Plain",
        standup_max_seconds=60,
        default_voice="Kore",
        tool_permissions=[],
        guidelines="Guidelines.",
    ),
}


def _agent(
    agent_id: str,
    role: str,
    *,
    allowed_repos: list[str] | None = None,
    allowed_jira_projects: list[str] | None = None,
    github_token: str = "tok-fake-github",
    jira_token: str = "tok-fake-jira",
) -> Agent:
    """Build an `Agent` directly (no file, no env var) for tests that do not exercise the loader."""
    return Agent(
        agent_id=agent_id,
        role=role,
        display_name=agent_id,
        github_username=f"{agent_id}-gh",
        jira_account_id=f"{agent_id}-acc",
        github_token_env=f"{agent_id.upper()}_GH",
        jira_token_env=f"{agent_id.upper()}_JIRA",
        allowed_repos=allowed_repos or [],
        allowed_jira_projects=allowed_jira_projects or [],
        github_token=SecretStr(github_token),
        jira_token=SecretStr(jira_token),
    )


@pytest.fixture(autouse=True)
def _assert_no_secret_leak(caplog):
    """Cross-cutting proof for every test in this module: `SECRET_TOKEN` never reaches a log."""
    caplog.set_level(logging.DEBUG)
    yield
    for record in caplog.records:
        assert SECRET_TOKEN not in record.getMessage()


# --- read actions: allowed ---


@pytest.mark.parametrize(
    ("action", "resource", "allowed_repos", "allowed_jira_projects"),
    [
        ("github:read", "org/repo", ["org/repo"], []),
        ("jira:read", "OPS", [], ["OPS"]),
    ],
)
def test_read_action_allowed_for_allowed_resource(
    action, resource, allowed_repos, allowed_jira_projects
):
    agent = _agent(
        "agent_a",
        "full_access",
        allowed_repos=allowed_repos,
        allowed_jira_projects=allowed_jira_projects,
    )

    require_permission(agent, action, resource, ROLES)  # does not raise


# --- read actions: denied because the resource is not allowed ---


@pytest.mark.parametrize(
    ("action", "resource", "allowed_repos", "allowed_jira_projects"),
    [
        ("github:read", "org/other-repo", ["org/repo"], []),
        ("jira:read", "SEC", [], ["OPS"]),
    ],
)
def test_read_action_denied_for_resource_outside_allowed_list(
    action, resource, allowed_repos, allowed_jira_projects
):
    agent = _agent(
        "agent_a",
        "full_access",
        allowed_repos=allowed_repos,
        allowed_jira_projects=allowed_jira_projects,
    )

    with pytest.raises(PermissionDenied):
        require_permission(agent, action, resource, ROLES)


# --- read actions: denied because the agent's role does not grant them ---


@pytest.mark.parametrize("action,resource", [("github:read", "org/repo"), ("jira:read", "OPS")])
def test_read_action_denied_when_role_lacks_permission(action, resource):
    # The resource IS allowed; only the role's tool_permissions is empty.
    agent = _agent(
        "agent_a", "no_access", allowed_repos=["org/repo"], allowed_jira_projects=["OPS"]
    )

    with pytest.raises(PermissionDenied):
        require_permission(agent, action, resource, ROLES)


# --- write actions: always denied, no exceptions ---


@pytest.mark.parametrize("action", ["github:write", "jira:write"])
@pytest.mark.parametrize("role", ["full_access", "no_access"])
def test_write_action_is_always_denied_regardless_of_role_or_resource(action, role):
    # allowed_repos/allowed_jira_projects DO contain the resource, and `role` may even have full
    # read access - write must still be denied, because no role's tool_permissions can grant it.
    resource = "org/repo" if action.startswith("github") else "OPS"
    agent = _agent("agent_a", role, allowed_repos=["org/repo"], allowed_jira_projects=["OPS"])

    with pytest.raises(PermissionDenied):
        require_permission(agent, action, resource, ROLES)


# --- zero requests happen when the guard denies ---


def test_denied_permission_results_in_zero_http_requests():
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"ok": True})

    transport = httpx2.MockTransport(handler)
    client = httpx2.Client(transport=transport)
    agent = _agent("agent_a", "full_access", allowed_repos=["org/repo"])

    def guarded_fetch() -> httpx2.Response:
        # The pattern #7/#8 will use: check the guard first, build/use the real client second.
        require_permission(agent, "github:read", "org/other-repo", ROLES)
        return client.get("https://api.github.com/repos/org/other-repo")

    try:
        with pytest.raises(PermissionDenied):
            guarded_fetch()
        assert calls == []
    finally:
        client.close()


# --- no shared token between two agents ---


def test_two_agents_never_share_each_others_resolved_token(tmp_path: Path, monkeypatch):
    agent_a_yaml = tmp_path / "agent_a.yaml"
    agent_a_yaml.write_text(
        "\n".join(
            [
                "agent_id: agent_a",
                "role: full_access",
                "display_name: Agent A",
                "github_username: agent-a-gh",
                "jira_account_id: agent-a-acc",
                "github_token_env: AGENT_A_GITHUB_TOKEN",
                "jira_token_env: AGENT_A_JIRA_TOKEN",
                "allowed_repos:",
                "  - org/repo-a",
                "allowed_jira_projects:",
                "  - OPS",
                "",
            ]
        ),
        encoding="utf-8",
    )
    agent_b_yaml = tmp_path / "agent_b.yaml"
    agent_b_yaml.write_text(
        "\n".join(
            [
                "agent_id: agent_b",
                "role: full_access",
                "display_name: Agent B",
                "github_username: agent-b-gh",
                "jira_account_id: agent-b-acc",
                "github_token_env: AGENT_B_GITHUB_TOKEN",
                "jira_token_env: AGENT_B_JIRA_TOKEN",
                "allowed_repos:",
                "  - org/repo-b",
                "allowed_jira_projects:",
                "  - SEC",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_A_GITHUB_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("AGENT_A_JIRA_TOKEN", "tok-a-jira")
    monkeypatch.setenv("AGENT_B_GITHUB_TOKEN", "tok-b-github")
    monkeypatch.setenv("AGENT_B_JIRA_TOKEN", "tok-b-jira")

    agent_a = load_agent(agent_a_yaml, ROLES)
    agent_b = load_agent(agent_b_yaml, ROLES)

    header_a = f"Bearer {agent_a.github_token.get_secret_value()}"
    header_b = f"Bearer {agent_b.github_token.get_secret_value()}"

    assert header_a == f"Bearer {SECRET_TOKEN}"
    assert agent_b.github_token.get_secret_value() not in header_a
    assert agent_a.github_token.get_secret_value() not in header_b
    assert SECRET_TOKEN not in header_b


# --- logging and no-leak ---


def test_permission_denied_is_logged_with_agent_id_and_resource_but_not_the_token(caplog):
    agent = _agent(
        "agent_a",
        "full_access",
        allowed_repos=["org/repo"],
        github_token=SECRET_TOKEN,
    )

    caplog.set_level(logging.WARNING, logger="rbaa.security")
    with pytest.raises(PermissionDenied) as exc_info:
        require_permission(agent, "github:read", "org/other-repo", ROLES)

    messages = [record.getMessage() for record in caplog.records]
    assert any("agent_a" in message and "org/other-repo" in message for message in messages)
    assert all(SECRET_TOKEN not in message for message in messages)
    assert SECRET_TOKEN not in str(exc_info.value)
    assert SECRET_TOKEN not in repr(exc_info.value)
