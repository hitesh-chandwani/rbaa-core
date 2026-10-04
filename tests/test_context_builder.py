"""Tests for `rbaa.context.build_work_context` (#9).

Every test is offline: `github_fetch`/`jira_fetch` are injected fakes (the public signature
accepts them for exactly this reason), so no HTTP is ever involved and the real fetchers are never
called. The wall clock is never read live: `clock` is always injected. One behaviour per test, per
`_docs/testing-guidelines.md`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator
from pydantic import SecretStr

from rbaa.agents import Agent
from rbaa.context import ContextUnavailable, build_work_context
from rbaa.integrations.github import (
    CommitActivity,
    GitHubActivity,
    GitHubFetchError,
    PullRequestActivity,
)
from rbaa.integrations.jira import JiraActivity, JiraBlocker, JiraFetchError, JiraTicket
from rbaa.roles import Role

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "work_context.schema.json"
SINCE = datetime(2024, 1, 1, tzinfo=UTC)
SECRET_GH_TOKEN = "SECRET_GH_TOKEN_999"
SECRET_JIRA_TOKEN = "SECRET_JIRA_TOKEN_999"


def _agent(
    agent_id: str = "bot",
    *,
    github_token: str = "tok-fake-github",
    jira_token: str = "tok-fake-jira",
) -> Agent:
    return Agent(
        agent_id=agent_id,
        role="full_access",
        display_name=agent_id,
        github_username="octobot",
        jira_account_id="bot-acc",
        github_token_env=f"{agent_id.upper()}_GH",
        jira_token_env=f"{agent_id.upper()}_JIRA",
        allowed_repos=["acme/ok"],
        allowed_jira_projects=["ABC"],
        github_token=SecretStr(github_token),
        jira_token=SecretStr(jira_token),
    )


ROLES: dict[str, Role] = {}


def _github_fetch_returning(activity: GitHubActivity, calls: list | None = None):
    async def _fetch(agent, roles, since):
        if calls is not None:
            calls.append(since)
        return activity

    return _fetch


def _github_fetch_raising(exc: Exception):
    async def _fetch(agent, roles, since):
        raise exc

    return _fetch


def _jira_fetch_returning(activity: JiraActivity, calls: list | None = None):
    async def _fetch(agent, roles, since):
        if calls is not None:
            calls.append(since)
        return activity

    return _fetch


def _jira_fetch_raising(exc: Exception):
    async def _fetch(agent, roles, since):
        raise exc

    return _fetch


def _empty_github_fetch():
    return _github_fetch_returning(GitHubActivity())


def _empty_jira_fetch():
    return _jira_fetch_returning(JiraActivity())


def _dump_json(result) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


# --- schema validation ---


async def test_output_validates_against_json_schema():
    github_activity = GitHubActivity(
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
        review_requests=[
            PullRequestActivity(
                id="acme/ok#44",
                title="Please review this refactor",
                url="https://github.com/acme/ok/pull/44",
                timestamp=datetime(2024, 2, 7, 15, 45, tzinfo=UTC),
            )
        ],
        errors=[GitHubFetchError(repo="acme/broken", status=404, message="Not Found")],
    )
    jira_activity = JiraActivity(
        tickets=[
            JiraTicket(
                key="ABC-12",
                summary="Investigate widget flakiness",
                status="Done",
                status_category="done",
                updated=datetime(2024, 2, 4, 9, 0, tzinfo=UTC),
                url="https://rbaa.atlassian.net/browse/ABC-12",
            )
        ],
        blockers=[JiraBlocker(key="ABC-12", blocking_key="ABC-9")],
        errors=[JiraFetchError(project="XYZ", status=403, message="denied")],
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(json.loads(_dump_json(result)))


async def test_truncated_present_for_every_category_even_when_untrimmed():
    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_empty_github_fetch(),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.truncated == {
        "commits": 0,
        "merged_prs": 0,
        "open_reviews": 0,
        "tickets": 0,
        "blockers": 0,
    }


# --- evidence ID formats ---


async def test_commit_evidence_id_format():
    github_activity = GitHubActivity(
        commits=[
            CommitActivity(
                id="deadbeef",
                title="t",
                url="https://github.com/acme/ok/commit/deadbeef",
                timestamp=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ]
    )
    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.commits[0].evidence_id == "gh:commit:deadbeef"


async def test_merged_pr_evidence_id_format_parsed_from_url():
    github_activity = GitHubActivity(
        merged_prs=[
            PullRequestActivity(
                id="acme/ok#42",
                title="t",
                url="https://github.com/acme/ok/pull/42",
                timestamp=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ]
    )
    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.merged_prs[0].evidence_id == "gh:pr:acme/ok#42"


async def test_open_review_evidence_id_format_parsed_from_url():
    github_activity = GitHubActivity(
        review_requests=[
            PullRequestActivity(
                id="acme/ok#44",
                title="t",
                url="https://github.com/acme/ok/pull/44",
                timestamp=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ]
    )
    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.open_reviews[0].evidence_id == "gh:pr:acme/ok#44"


async def test_ticket_evidence_id_format():
    jira_activity = JiraActivity(
        tickets=[
            JiraTicket(
                key="ABC-12",
                summary="s",
                status="Done",
                status_category="done",
                updated=datetime(2024, 2, 1, tzinfo=UTC),
                url="https://rbaa.atlassian.net/browse/ABC-12",
            )
        ]
    )
    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_empty_github_fetch(),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    assert result.tickets[0].evidence_id == "jira:ABC-12"


async def test_blocker_evidence_id_is_blocked_ticket_key_not_blocking_key():
    jira_activity = JiraActivity(
        tickets=[
            JiraTicket(
                key="ABC-12",
                summary="s",
                status="Blocked",
                status_category="indeterminate",
                updated=datetime(2024, 2, 1, tzinfo=UTC),
                url="https://rbaa.atlassian.net/browse/ABC-12",
            )
        ],
        blockers=[JiraBlocker(key="ABC-12", blocking_key="ABC-9")],
    )
    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_empty_github_fetch(),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    assert result.blockers[0].evidence_id == "jira:ABC-12"
    assert result.blockers[0].blocking_key == "ABC-9"


# --- determinism ---


async def test_same_inputs_and_clock_give_byte_identical_json():
    def make_github_activity() -> GitHubActivity:
        return GitHubActivity(
            commits=[
                CommitActivity(
                    id="z9",
                    title="t1",
                    url="https://github.com/acme/ok/commit/z9",
                    timestamp=datetime(2024, 2, 3, tzinfo=UTC),
                ),
                CommitActivity(
                    id="a1",
                    title="t2",
                    url="https://github.com/acme/ok/commit/a1",
                    timestamp=datetime(2024, 2, 1, tzinfo=UTC),
                ),
            ],
            merged_prs=[
                PullRequestActivity(
                    id="acme/ok#42",
                    title="Merged",
                    url="https://github.com/acme/ok/pull/42",
                    timestamp=datetime(2024, 2, 5, tzinfo=UTC),
                )
            ],
        )

    def make_jira_activity() -> JiraActivity:
        return JiraActivity(
            tickets=[
                JiraTicket(
                    key="ABC-12",
                    summary="s",
                    status="Done",
                    status_category="done",
                    updated=datetime(2024, 2, 4, tzinfo=UTC),
                    url="https://rbaa.atlassian.net/browse/ABC-12",
                )
            ]
        )

    fixed_clock = lambda: datetime(2024, 3, 1, 12, 0, tzinfo=UTC)  # noqa: E731

    agent = _agent()
    first = await build_work_context(
        agent,
        ROLES,
        SINCE,
        clock=fixed_clock,
        github_fetch=_github_fetch_returning(make_github_activity()),
        jira_fetch=_jira_fetch_returning(make_jira_activity()),
    )
    second = await build_work_context(
        agent,
        ROLES,
        SINCE,
        clock=fixed_clock,
        github_fetch=_github_fetch_returning(make_github_activity()),
        jira_fetch=_jira_fetch_returning(make_jira_activity()),
    )

    assert _dump_json(first) == _dump_json(second)
    # And the commits are ordered by evidence_id ascending, not fetch order.
    assert [c.evidence_id for c in first.commits] == ["gh:commit:a1", "gh:commit:z9"]


# --- since default chain ---


async def test_last_standup_at_overrides_computed_default():
    last_standup_at = datetime(2024, 5, 10, 8, 30, tzinfo=UTC)
    seen_since: list[datetime] = []

    result = await build_work_context(
        _agent(),
        ROLES,
        last_standup_at=last_standup_at,
        clock=lambda: datetime(2024, 6, 1, tzinfo=UTC),  # should be ignored
        github_fetch=_github_fetch_returning(GitHubActivity(), seen_since),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.since == last_standup_at
    assert seen_since == [last_standup_at]  # propagated to the fetcher, unchanged


async def test_explicit_since_overrides_last_standup_at():
    explicit_since = datetime(2024, 7, 1, tzinfo=UTC)
    result = await build_work_context(
        _agent(),
        ROLES,
        explicit_since,
        last_standup_at=datetime(2024, 5, 10, tzinfo=UTC),
        clock=lambda: datetime(2024, 6, 1, tzinfo=UTC),
        github_fetch=_empty_github_fetch(),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.since == explicit_since


async def test_monday_defaults_to_previous_friday_in_timezone():
    monday_9am_nyc = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("America/New_York"))

    result = await build_work_context(
        _agent(),
        ROLES,
        timezone="America/New_York",
        clock=lambda: monday_9am_nyc,
        github_fetch=_empty_github_fetch(),
        jira_fetch=_empty_jira_fetch(),
    )

    expected = datetime(2026, 10, 2, 0, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    assert result.since == expected
    assert result.since.utcoffset().total_seconds() == -4 * 3600


# --- partial failure ---


async def test_github_failing_adds_error_entry_and_keeps_jira_data():
    jira_activity = JiraActivity(
        tickets=[
            JiraTicket(
                key="ABC-1",
                summary="s",
                status="Done",
                status_category="done",
                updated=datetime(2024, 2, 1, tzinfo=UTC),
                url="https://rbaa.atlassian.net/browse/ABC-1",
            )
        ]
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_raising(RuntimeError("github is down")),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    assert result.commits == []
    assert result.merged_prs == []
    assert result.open_reviews == []
    assert len(result.tickets) == 1  # jira's data is populated normally
    assert [e.source for e in result.errors] == ["github"]
    assert result.errors[0].message == "github is down"
    assert result.errors[0].resource is None


async def test_jira_failing_adds_error_entry_and_keeps_github_data():
    github_activity = GitHubActivity(
        commits=[
            CommitActivity(
                id="a1",
                title="t",
                url="https://github.com/acme/ok/commit/a1",
                timestamp=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ]
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_jira_fetch_raising(RuntimeError("jira is down")),
    )

    assert result.tickets == []
    assert result.blockers == []
    assert len(result.commits) == 1  # github's data is populated normally
    assert [e.source for e in result.errors] == ["jira"]
    assert result.errors[0].message == "jira is down"
    assert result.errors[0].resource is None


async def test_both_sources_failing_raises_context_unavailable():
    github_exc = RuntimeError("github is down")
    jira_exc = RuntimeError("jira is down")

    with pytest.raises(ContextUnavailable) as exc_info:
        await build_work_context(
            _agent(),
            ROLES,
            SINCE,
            clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
            github_fetch=_github_fetch_raising(github_exc),
            jira_fetch=_jira_fetch_raising(jira_exc),
        )

    assert exc_info.value.github_error is github_exc
    assert exc_info.value.jira_error is jira_exc


async def test_per_repo_github_errors_are_unioned_tagged_github():
    github_activity = GitHubActivity(
        errors=[GitHubFetchError(repo="acme/broken", status=404, message="Not Found")]
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_empty_jira_fetch(),
    )

    assert len(result.errors) == 1
    assert result.errors[0].source == "github"
    assert result.errors[0].status == 404
    assert result.errors[0].resource == "acme/broken"


# --- cross-linking ---


async def test_pr_title_mentioning_known_ticket_key_links_both_ways():
    github_activity = GitHubActivity(
        merged_prs=[
            PullRequestActivity(
                id="acme/ok#42",
                title="Fix the flaky widget (ABC-12)",
                url="https://github.com/acme/ok/pull/42",
                timestamp=datetime(2024, 2, 5, tzinfo=UTC),
            )
        ]
    )
    jira_activity = JiraActivity(
        tickets=[
            JiraTicket(
                key="ABC-12",
                summary="s",
                status="Done",
                status_category="done",
                updated=datetime(2024, 2, 4, tzinfo=UTC),
                url="https://rbaa.atlassian.net/browse/ABC-12",
            )
        ]
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    assert result.merged_prs[0].related == ["jira:ABC-12"]
    assert result.tickets[0].related == ["gh:pr:acme/ok#42"]


async def test_pr_title_mentioning_unknown_key_does_not_link():
    github_activity = GitHubActivity(
        merged_prs=[
            PullRequestActivity(
                id="acme/ok#42",
                title="Mentions ZZZ-99 which is not a fetched ticket",
                url="https://github.com/acme/ok/pull/42",
                timestamp=datetime(2024, 2, 5, tzinfo=UTC),
            )
        ]
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_empty_jira_fetch(),
    )

    assert result.merged_prs[0].related == []


# --- per-category truncation ---


async def test_category_truncation_keeps_most_recent_then_sorts_by_evidence_id():
    github_activity = GitHubActivity(
        commits=[
            CommitActivity(
                id="old",
                title="t",
                url="https://github.com/acme/ok/commit/old",
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            CommitActivity(
                id="mid",
                title="t",
                url="https://github.com/acme/ok/commit/mid",
                timestamp=datetime(2024, 1, 2, tzinfo=UTC),
            ),
            CommitActivity(
                id="new",
                title="t",
                url="https://github.com/acme/ok/commit/new",
                timestamp=datetime(2024, 1, 3, tzinfo=UTC),
            ),
        ]
    )

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        max_per_category=2,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_empty_jira_fetch(),
    )

    # "old" (2024-01-01) is dropped; "mid" and "new" are kept, then re-sorted by evidence_id.
    assert [c.evidence_id for c in result.commits] == ["gh:commit:mid", "gh:commit:new"]
    assert result.truncated["commits"] == 1
    assert result.truncated["merged_prs"] == 0


async def test_blockers_capped_by_their_tickets_updated_timestamp():
    tickets = [
        JiraTicket(
            key=f"ABC-{n}",
            summary="s",
            status="Blocked",
            status_category="indeterminate",
            updated=datetime(2024, 1, n, tzinfo=UTC),
            url=f"https://rbaa.atlassian.net/browse/ABC-{n}",
        )
        for n in (1, 2, 3)
    ]
    blockers = [JiraBlocker(key=f"ABC-{n}", blocking_key=None) for n in (1, 2, 3)]
    jira_activity = JiraActivity(tickets=tickets, blockers=blockers)

    result = await build_work_context(
        _agent(),
        ROLES,
        SINCE,
        max_per_category=2,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_empty_github_fetch(),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    # ABC-1's ticket.updated (Jan 1) is the oldest, so its blocker is dropped.
    assert [b.evidence_id for b in result.blockers] == ["jira:ABC-2", "jira:ABC-3"]
    assert result.truncated["blockers"] == 1


# --- no credentials ---


async def test_no_credentials_in_serialized_json():
    github_activity = GitHubActivity(
        commits=[
            CommitActivity(
                id="a1",
                title="t",
                url="https://github.com/acme/ok/commit/a1",
                timestamp=datetime(2024, 2, 1, tzinfo=UTC),
            )
        ]
    )
    jira_activity = JiraActivity(
        tickets=[
            JiraTicket(
                key="ABC-1",
                summary="s",
                status="Done",
                status_category="done",
                updated=datetime(2024, 2, 1, tzinfo=UTC),
                url="https://rbaa.atlassian.net/browse/ABC-1",
            )
        ]
    )
    agent = _agent(github_token=SECRET_GH_TOKEN, jira_token=SECRET_JIRA_TOKEN)

    result = await build_work_context(
        agent,
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_returning(github_activity),
        jira_fetch=_jira_fetch_returning(jira_activity),
    )

    payload = _dump_json(result)
    assert SECRET_GH_TOKEN not in payload
    assert SECRET_JIRA_TOKEN not in payload
    assert SECRET_GH_TOKEN not in repr(result)
    assert SECRET_JIRA_TOKEN not in repr(result)


async def test_no_credentials_even_when_a_source_fails():
    agent = _agent(github_token=SECRET_GH_TOKEN, jira_token=SECRET_JIRA_TOKEN)

    result = await build_work_context(
        agent,
        ROLES,
        SINCE,
        clock=lambda: datetime(2024, 3, 1, tzinfo=UTC),
        github_fetch=_github_fetch_raising(RuntimeError("github is down")),
        jira_fetch=_empty_jira_fetch(),
    )

    payload = _dump_json(result)
    assert SECRET_GH_TOKEN not in payload
    assert SECRET_JIRA_TOKEN not in payload
