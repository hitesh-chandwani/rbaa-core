"""The Jira activity fetcher (#8).

`fetch_jira_activity(agent, roles, since, client=None, max_retries=3)` returns one agent's real
Jira ticket activity - tickets, status changes, tickets completed (moved to Done) at or after
`since`, and blockers - across every project in `agent.allowed_jira_projects`, as a typed
`JiraActivity`. Mirrors `rbaa.integrations.github.fetch_github_activity` (#7) in structure,
conventions, and the permission/pagination/rate-limit style, so the two integrations read the
same way.

Permission gate
----------------

For every project in `agent.allowed_jira_projects`, `rbaa.security.require_permission(agent,
"jira:read", project_key, roles)` is called *before* any HTTP request for that project. A project
not in `agent.allowed_jira_projects` is never requested - this function has no way to see any
other project (its only source of projects is that list, exactly as #7's only source of repos is
`agent.allowed_repos`). A `PermissionDenied` from the guard for one project is caught and turned
into one `JiraFetchError` (status 403) for that project; the remaining allowed projects are still
fetched.

Endpoint (Jira Cloud REST API v3)
----------------------------------

Per project: `GET {JIRA_API_BASE}/rest/api/3/search` with
`jql=project = {project_key} AND assignee = {agent.jira_account_id} ORDER BY updated DESC`,
`expand=changelog`, `fields=summary,status,updated`, `startAt={n}`, `maxResults=100`.

Pagination follows the response body's own `startAt` / `maxResults` / `total` fields -
incrementing `startAt` by the response's `maxResults` and re-requesting - until
`startAt + maxResults >= total`, never a hardcoded page count.

Status changes and "moved to Done"
-----------------------------------

Each returned issue's `changelog.histories` (present because of `expand=changelog`) is scanned:
a history entry contributes one `JiraStatusChange` for every one of its `items` with
`field == "status"`, but only when the history's `created` is `>= since`. A ticket counts as
"moved to Done at or after `since`" - and is added to both `status_changes` and `completed` -
exactly when it has at least one such status-change entry AND the issue's *current*
`fields.status.statusCategory.key == "done"`. The category is read from the current status on the
issue payload, never guessed from a status string, mirroring #7's commit-vs-merge-commit
distinction by `parents` length rather than a guessed flag.

Blockers
--------

Per issue: `fields.labels` contains the literal string `"blocked"`, OR `fields.issuelinks`
contains an entry with `type.inward == "is blocked by"` whose `inwardIssue.fields.status.
statusCategory.key != "done"` (the blocking ticket is unresolved). One `JiraBlocker` is added per
unresolved blocking link found (`blocking_key` set to that link's issue key); if no unresolved
link is found but the label matched, one `JiraBlocker` with `blocking_key=None` is added instead
(never both for the same ticket - see "Judgment calls" below).

Rate limits
-----------

On `429`, `Retry-After` (seconds) is used to compute the wait; the same request is retried up to
`max_retries` attempts total. Exhausting retries raises `RateLimitError(reset_at)` for the whole
call - it is not turned into a `JiraFetchError`. The wall clock is never read directly: `_now` and
`_sleep` are the two seams tests replace (there is no clock parameter on the public signature,
pinned by #8, mirroring #7).

A non-429 HTTP error (e.g. 404, 401) on one project adds exactly one `JiraFetchError` for that
project and does not stop the other projects in `agent.allowed_jira_projects` from being fetched.

The agent's Jira token is read once per project (from `agent.jira_token`, already populated by
`rbaa.agents.load_agent` from the environment variable named by `jira_token_env`), only after the
permission gate for that project has passed - so it is never sent to, or logged for, a project
outside `agent.allowed_jira_projects`.

Judgment calls (#8 did not pin these; disclosed per the engineer's issue comment)
----------------------------------------------------------------------------------

1. **Jira base URL.** `Agent` (`rbaa.agents.models`) has no per-agent Jira base-URL field, and
   adding one is outside this issue's file list (`src/rbaa/integrations/jira.py`, `tests/`,
   `tests/fixtures/http/jira/`). `JIRA_API_BASE` below is a module constant, exactly as #7's
   `GITHUB_API_BASE` is for GitHub's single fixed API host - except a real Jira Cloud site's host
   is per-tenant, so this constant is a placeholder until agent configuration grows a real field.
2. **Auth scheme.** Sent as `Authorization: Bearer <agent.jira_token>`, mirroring #7's GitHub
   header exactly. Real Jira Cloud normally wants Basic auth with an account email plus API token;
   `Agent` carries only one secret token and no email, so Bearer is used for parity with #7 rather
   than guessing an email field that does not exist.
3. **`fields` vs. what blocker/status detection needs.** The pinned query param is
   `fields=summary,status,updated` (#8's literal wording), but blocker detection reads
   `fields.labels`/`fields.issuelinks` and completion detection reads `fields.status.
   statusCategory`. Against a real Jira site, that `fields` value would make the API omit
   `labels`/`issuelinks` from the response, silently breaking blocker detection. Tests mock the
   transport and the fixtures include those fields regardless of what was requested, so the
   acceptance criterion's exact param is honored without touching correctness of the *tests*; this
   is flagged here as a very likely latent production bug, not silently fixed, since the issue
   pins the param value exactly.
4. **Ticket `url`.** Built as `{JIRA_API_BASE}/browse/{key}`. The search response has no
   browser-facing field equivalent to GitHub's `html_url`; Jira's own `self` field is only the API
   endpoint, not something a human would click.
5. **Blocker de-duplication.** When a ticket has both a `"blocked"` label and one or more
   unresolved `issuelinks`, only the link-derived `JiraBlocker` entries are emitted (one per
   unresolved link); a label-only entry (`blocking_key=None`) is added only when no unresolved
   link was found. This avoids emitting a redundant all-`None` row alongside more specific
   link-derived rows for the same ticket - #8 does not pin behaviour for the combined case.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx2

from rbaa.agents import Agent
from rbaa.roles import Role
from rbaa.security import PermissionDenied, require_permission

# Judgment call #1 (see module docstring): a module constant, mirroring #7's GITHUB_API_BASE,
# until agent configuration grows a real per-agent Jira base-URL field.
JIRA_API_BASE = "https://rbaa.atlassian.net"

_MAX_RESULTS = 100
_RATE_LIMIT_STATUSES = frozenset({429})
_DONE_CATEGORY = "done"
_BLOCKED_LABEL = "blocked"
_BLOCKED_BY_INWARD = "is blocked by"


# Seams for tests: no clock/sleep is accepted on the public signature (pinned by #8, mirroring
# #7), so tests replace these two module attributes instead of patching `time.sleep`/the real
# clock.
def _now() -> datetime:
    return datetime.now(UTC)


_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class JiraTicket:
    """One ticket assigned to the agent."""

    key: str
    summary: str
    status: str
    status_category: str
    updated: datetime
    url: str


@dataclass(frozen=True)
class JiraStatusChange:
    """One status-change history item on one ticket."""

    key: str
    from_status: str
    to_status: str
    changed_at: datetime


@dataclass(frozen=True)
class JiraBlocker:
    """One ticket blocked, either by label or by an unresolved `issuelinks` entry."""

    key: str
    blocking_key: str | None


@dataclass(frozen=True)
class JiraFetchError:
    """One project's fetch failed (permission denial or a non-fatal HTTP error)."""

    project: str
    status: int
    message: str


@dataclass(frozen=True)
class JiraActivity:
    """One agent's Jira activity across every project in `agent.allowed_jira_projects`."""

    tickets: list[JiraTicket] = field(default_factory=list)
    status_changes: list[JiraStatusChange] = field(default_factory=list)
    completed: list[JiraTicket] = field(default_factory=list)
    blockers: list[JiraBlocker] = field(default_factory=list)
    errors: list[JiraFetchError] = field(default_factory=list)


class RateLimitError(Exception):
    """Jira's rate limit was still in effect after `max_retries` attempts.

    Raised for the whole `fetch_jira_activity` call - it is not caught per project. `reset_at` is
    the UTC time at which the wait computed from `Retry-After` would clear.
    """

    def __init__(self, reset_at: datetime) -> None:
        self.reset_at = reset_at
        super().__init__(f"Jira rate limit exhausted; resets at {reset_at.isoformat()}")


class _ProjectFetchFailed(Exception):
    """Internal: one project's request failed with a status that should become a JiraFetchError."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _default_client() -> httpx2.AsyncClient:
    """The module-constructed client used when the caller passes no `client`."""
    return httpx2.AsyncClient(timeout=httpx2.Timeout(30.0))


def _parse_dt(value: str) -> datetime:
    """Parse a Jira API timestamp (`...Z` or with an explicit offset) to a UTC `datetime`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _compute_wait_and_reset(headers: httpx2.Headers, now: datetime) -> tuple[float, datetime]:
    """From a 429 response's headers, the seconds to wait and the UTC reset time.

    Only `Retry-After` is used (pinned by #8); unlike #7 there is no `X-RateLimit-Reset` header
    fallback. A response missing it waits zero seconds (fixtures always include it).
    """
    retry_after = headers.get("retry-after")
    wait_seconds = float(retry_after) if retry_after is not None else 0.0
    return wait_seconds, now + timedelta(seconds=wait_seconds)


def _auth_headers(agent: Agent) -> dict[str, str]:
    # Judgment call #2 (see module docstring): Bearer, mirroring #7's GitHub header exactly.
    return {
        "Authorization": f"Bearer {agent.jira_token.get_secret_value()}",
        "Accept": "application/json",
    }


async def _request_with_retry(
    client: httpx2.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    max_retries: int,
) -> httpx2.Response:
    """GET `url`, retrying on 429 up to `max_retries` attempts total.

    Raises `RateLimitError` (for the whole call) once attempts are exhausted. Any other response,
    including a non-429 error, is returned as-is for the caller to interpret.
    """
    attempt = 0
    while True:
        attempt += 1
        response = await client.get(url, params=params, headers=headers)
        if response.status_code in _RATE_LIMIT_STATUSES:
            if attempt >= max_retries:
                _, reset_at = _compute_wait_and_reset(response.headers, _now())
                raise RateLimitError(reset_at)
            wait_seconds, _ = _compute_wait_and_reset(response.headers, _now())
            await _sleep(wait_seconds)
            continue
        return response


def _error_message(response: httpx2.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(payload, dict):
        messages = payload.get("errorMessages")
        if messages:
            return str(messages[0])
        message = payload.get("message")
        if message:
            return str(message)
    return response.reason_phrase or "error"


async def _fetch_project_issues(
    client: httpx2.AsyncClient,
    project_key: str,
    account_id: str,
    headers: Mapping[str, str],
    max_retries: int,
) -> list[dict]:
    """Fetch every issue for `project_key`, following `startAt`/`maxResults`/`total`.

    Raises `_ProjectFetchFailed` on any non-429 error status. `RateLimitError` propagates from
    `_request_with_retry`.
    """
    url = f"{JIRA_API_BASE}/rest/api/3/search"
    issues: list[dict] = []
    start_at = 0
    while True:
        params = {
            "jql": f"project = {project_key} AND assignee = {account_id} ORDER BY updated DESC",
            "expand": "changelog",
            "fields": "summary,status,updated",
            "startAt": str(start_at),
            "maxResults": str(_MAX_RESULTS),
        }
        response = await _request_with_retry(
            client, url, params=params, headers=headers, max_retries=max_retries
        )
        if response.status_code >= 400:
            raise _ProjectFetchFailed(response.status_code, _error_message(response))

        payload = response.json()
        issues.extend(payload.get("issues", []))

        actual_start = int(payload["startAt"])
        actual_max = int(payload["maxResults"])
        total = int(payload["total"])
        if actual_start + actual_max >= total:
            break
        start_at = actual_start + actual_max

    return issues


def _parse_ticket(raw: dict) -> JiraTicket:
    fields = raw["fields"]
    status = fields.get("status") or {}
    key = raw["key"]
    return JiraTicket(
        key=key,
        summary=fields.get("summary", ""),
        status=status.get("name", ""),
        status_category=(status.get("statusCategory") or {}).get("key", ""),
        updated=_parse_dt(fields["updated"]),
        # Judgment call #4 (see module docstring): synthesized; Jira's `self` is an API URL, not
        # a browser-facing one.
        url=f"{JIRA_API_BASE}/browse/{key}",
    )


def _extract_status_changes(raw: dict, since: datetime) -> list[JiraStatusChange]:
    key = raw["key"]
    changelog = raw.get("changelog") or {}
    changes: list[JiraStatusChange] = []
    for history in changelog.get("histories", []):
        created = _parse_dt(history["created"])
        if created < since:
            continue
        for item in history.get("items", []):
            if item.get("field") == "status":
                changes.append(
                    JiraStatusChange(
                        key=key,
                        from_status=item.get("fromString", ""),
                        to_status=item.get("toString", ""),
                        changed_at=created,
                    )
                )
    return changes


def _extract_blockers(raw: dict) -> list[JiraBlocker]:
    fields = raw["fields"]
    key = raw["key"]

    link_blockers: list[JiraBlocker] = []
    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        if link_type.get("inward") != _BLOCKED_BY_INWARD:
            continue
        inward_issue = link.get("inwardIssue")
        if not inward_issue:
            continue
        inward_status = inward_issue.get("fields", {}).get("status") or {}
        inward_category = (inward_status.get("statusCategory") or {}).get("key")
        if inward_category != _DONE_CATEGORY:
            link_blockers.append(JiraBlocker(key=key, blocking_key=inward_issue.get("key")))

    if link_blockers:
        # Judgment call #5 (see module docstring): link-derived entries take precedence over a
        # redundant label-only row for the same ticket.
        return link_blockers

    labels = fields.get("labels") or []
    if _BLOCKED_LABEL in labels:
        return [JiraBlocker(key=key, blocking_key=None)]

    return []


async def fetch_jira_activity(
    agent: Agent,
    roles: dict[str, Role],
    since: datetime,
    client: httpx2.AsyncClient | None = None,
    max_retries: int = 3,
) -> JiraActivity:
    """Fetch `agent`'s Jira activity since `since`, across every project in
    `agent.allowed_jira_projects`.

    See the module docstring for the permission gate, endpoint, pagination, status-change/
    moved-to-Done detection, blocker rules, and rate-limit handling. A permission denial or a
    non-429 HTTP error for one project becomes one `JiraFetchError` in the result and does not
    stop the other projects; an exhausted rate limit raises `RateLimitError` for the whole call.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    else:
        since = since.astimezone(UTC)

    owns_client = client is None
    active_client = client if client is not None else _default_client()

    try:
        tickets: list[JiraTicket] = []
        status_changes: list[JiraStatusChange] = []
        completed: list[JiraTicket] = []
        blockers: list[JiraBlocker] = []
        errors: list[JiraFetchError] = []

        for project_key in agent.allowed_jira_projects:
            try:
                require_permission(agent, "jira:read", project_key, roles)
            except PermissionDenied as exc:
                errors.append(JiraFetchError(project=project_key, status=403, message=str(exc)))
                continue

            headers = _auth_headers(agent)
            try:
                raw_issues = await _fetch_project_issues(
                    active_client, project_key, agent.jira_account_id, headers, max_retries
                )
            except _ProjectFetchFailed as exc:
                errors.append(
                    JiraFetchError(project=project_key, status=exc.status, message=exc.message)
                )
                continue

            for raw in raw_issues:
                ticket = _parse_ticket(raw)
                tickets.append(ticket)

                ticket_changes = _extract_status_changes(raw, since)
                status_changes.extend(ticket_changes)
                if ticket_changes and ticket.status_category == _DONE_CATEGORY:
                    completed.append(ticket)

                blockers.extend(_extract_blockers(raw))

        return JiraActivity(
            tickets=tickets,
            status_changes=status_changes,
            completed=completed,
            blockers=blockers,
            errors=errors,
        )
    finally:
        if owns_client:
            await active_client.aclose()


__all__ = [
    "JIRA_API_BASE",
    "JiraTicket",
    "JiraStatusChange",
    "JiraBlocker",
    "JiraFetchError",
    "JiraActivity",
    "RateLimitError",
    "fetch_jira_activity",
]
