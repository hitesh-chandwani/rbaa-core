"""The GitHub activity fetcher (#7).

`fetch_github_activity(agent, roles, since, client=None, max_retries=3)` returns one agent's
real GitHub activity - commits, merged PRs, open PRs, and PRs awaiting the agent's review -
across every repo in `agent.allowed_repos`, as a typed `GitHubActivity`.

Permission gate
----------------

For every repo in `agent.allowed_repos`, `rbaa.security.require_permission(agent, "github:read",
repo, roles)` is called *before* any HTTP request for that repo. A repo not in
`agent.allowed_repos` is never requested - this function has no way to see any other repo. A
`PermissionDenied` from the guard for one repo is caught and turned into one `GitHubFetchError`
(status 403) for that repo; the remaining allowed repos are still fetched.

Endpoints (GitHub REST API v3)
-------------------------------

- Commits: `GET /repos/{owner}/{repo}/commits?author={github_username}&since={since.isoformat()}
  &per_page=100&page={n}`.
- Merged PRs / open PRs / review requests: `GET /search/issues` with, respectively,
  `q=repo:{owner}/{repo} type:pr author:{github_username} is:merged`,
  `q=repo:{owner}/{repo} type:pr author:{github_username} is:open`, and
  `q=repo:{owner}/{repo} review-requested:{github_username} is:open`, each with
  `per_page=100&page={n}`. The search API has no reliable server-side `since`, so results are
  filtered client-side to `updated_at >= since`.

Pagination follows the response's `Link` header (`rel="next"`) until it is absent - never a
hardcoded page count.

Merge commits are excluded by the commit object's `parents` array (`len(parents) >= 2`), not by
any PR-level `merge` flag: that marks PRs, and the commits endpoint does not return it.

Rate limits
-----------

On `403` or `429`, `Retry-After` (seconds) is used if present, else `X-RateLimit-Reset` (epoch
seconds) to compute a wait; the same request is retried up to `max_retries` attempts total.
Exhausting retries raises `RateLimitError(reset_at)` for the whole call - it is not turned into a
`GitHubFetchError`.

The wall clock is never read directly in this logic: `_now` and `_sleep` are the two seams tests
replace (there is no clock parameter on the public signature, which is pinned by #7), so a test
can run the retry loop without really waiting.

A `404` on one repo (from either endpoint) adds exactly one `GitHubFetchError` for that repo and
does not stop the other repos in `agent.allowed_repos` from being fetched.

The agent's GitHub token is read once per repo, from `agent.github_token` (already populated by
`rbaa.agents.load_agent` from the environment variable named by `github_token_env`), only after
the permission gate for that repo has passed - so it is never sent to, or logged for, a repo
outside `agent.allowed_repos`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx2

from rbaa.agents import Agent
from rbaa.roles import Role
from rbaa.security import PermissionDenied, require_permission

GITHUB_API_BASE = "https://api.github.com"
_PER_PAGE = 100
_RATE_LIMIT_STATUSES = frozenset({403, 429})
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


# Seams for tests: no clock/sleep is accepted on the public signature (pinned by #7), so tests
# replace these two module attributes instead of patching `time.sleep`/the real clock.
def _now() -> datetime:
    return datetime.now(UTC)


_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class CommitActivity:
    """One commit authored by the agent's GitHub user."""

    id: str
    title: str
    url: str
    timestamp: datetime


@dataclass(frozen=True)
class PullRequestActivity:
    """One pull request: merged, open, or awaiting the agent's review."""

    id: str
    title: str
    url: str
    timestamp: datetime


@dataclass(frozen=True)
class GitHubFetchError:
    """One repo's fetch failed (permission denial or a non-fatal HTTP error)."""

    repo: str
    status: int
    message: str


@dataclass(frozen=True)
class GitHubActivity:
    """One agent's GitHub activity across every repo in `agent.allowed_repos`."""

    commits: list[CommitActivity] = field(default_factory=list)
    merged_prs: list[PullRequestActivity] = field(default_factory=list)
    open_prs: list[PullRequestActivity] = field(default_factory=list)
    review_requests: list[PullRequestActivity] = field(default_factory=list)
    errors: list[GitHubFetchError] = field(default_factory=list)


class RateLimitError(Exception):
    """GitHub's rate limit was still in effect after `max_retries` attempts.

    Raised for the whole `fetch_github_activity` call - it is not caught per repo. `reset_at` is
    the UTC time at which GitHub said the limit would clear.
    """

    def __init__(self, reset_at: datetime) -> None:
        self.reset_at = reset_at
        super().__init__(f"GitHub rate limit exhausted; resets at {reset_at.isoformat()}")


class _RepoFetchFailed(Exception):
    """Internal: one repo's request failed with a status that should become a `GitHubFetchError`."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _default_client() -> httpx2.AsyncClient:
    """The module-constructed client used when the caller passes no `client`."""
    return httpx2.AsyncClient(timeout=httpx2.Timeout(30.0))


def _parse_dt(value: str) -> datetime:
    """Parse a GitHub API timestamp (`...Z` or with an explicit offset) to a UTC `datetime`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _parse_link_header(value: str | None) -> dict[str, str]:
    """`Link: <url1>; rel="next", <url2>; rel="last"` -> `{"next": url1, "last": url2}`."""
    if not value:
        return {}
    return {rel: url for url, rel in _LINK_RE.findall(value)}


def _compute_wait_and_reset(headers: httpx2.Headers, now: datetime) -> tuple[float, datetime]:
    """From a 403/429 response's headers, the seconds to wait and the UTC reset time."""
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        wait_seconds = float(retry_after)
        return wait_seconds, now + timedelta(seconds=wait_seconds)

    reset_header = headers.get("x-ratelimit-reset")
    if reset_header is not None:
        reset_at = datetime.fromtimestamp(int(reset_header), tz=UTC)
        wait_seconds = max((reset_at - now).total_seconds(), 0.0)
        return wait_seconds, reset_at

    # Neither header present: nothing to wait on. Fixtures always include one of the two.
    return 0.0, now


def _auth_headers(agent: Agent) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {agent.github_token.get_secret_value()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _request_with_retry(
    client: httpx2.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str] | None,
    headers: Mapping[str, str],
    max_retries: int,
) -> httpx2.Response:
    """GET `url`, retrying on 403/429 up to `max_retries` attempts total.

    Raises `RateLimitError` (for the whole call) once attempts are exhausted. Any other response,
    including 404, is returned as-is for the caller to interpret.
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
    message = payload.get("message") if isinstance(payload, dict) else None
    return message or response.reason_phrase or "error"


async def _fetch_all_pages(
    client: httpx2.AsyncClient,
    url: str,
    params: dict[str, str],
    *,
    headers: Mapping[str, str],
    max_retries: int,
    extract_items: Callable[[object], list[dict]],
) -> list[dict]:
    """Follow `Link: rel="next"` until it is absent, collecting every page's items.

    Raises `_RepoFetchFailed` on a 404. `RateLimitError` propagates from `_request_with_retry`.
    """
    items: list[dict] = []
    next_url: str | None = url
    next_params: dict[str, str] | None = params
    while next_url is not None:
        response = await _request_with_retry(
            client, next_url, params=next_params, headers=headers, max_retries=max_retries
        )
        if response.status_code == 404:
            raise _RepoFetchFailed(404, _error_message(response))
        response.raise_for_status()
        items.extend(extract_items(response.json()))
        next_url = _parse_link_header(response.headers.get("link")).get("next")
        next_params = None
    return items


def _parse_commit(raw: dict, username: str, since: datetime) -> CommitActivity | None:
    parents = raw.get("parents") or []
    if len(parents) >= 2:
        return None  # merge commit

    author = raw.get("author") or {}
    if author.get("login") != username:
        return None  # not this agent's commit (defensive, in addition to `author=`)

    timestamp = _parse_dt(raw["commit"]["author"]["date"])
    if timestamp < since:
        return None

    message = raw.get("commit", {}).get("message", "")
    title = message.splitlines()[0] if message else ""
    return CommitActivity(
        id=raw["sha"], title=title, url=raw.get("html_url", ""), timestamp=timestamp
    )


def _parse_pull_request(
    raw: dict, owner: str, repo: str, since: datetime
) -> PullRequestActivity | None:
    timestamp = _parse_dt(raw["updated_at"])
    if timestamp < since:
        return None
    return PullRequestActivity(
        id=f"{owner}/{repo}#{raw['number']}",
        title=raw.get("title", ""),
        url=raw.get("html_url", ""),
        timestamp=timestamp,
    )


async def _fetch_commits(
    client: httpx2.AsyncClient,
    owner: str,
    repo: str,
    username: str,
    since: datetime,
    headers: Mapping[str, str],
    max_retries: int,
) -> list[CommitActivity]:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits"
    params = {
        "author": username,
        "since": since.isoformat(),
        "per_page": str(_PER_PAGE),
        "page": "1",
    }
    raw_commits = await _fetch_all_pages(
        client,
        url,
        params,
        headers=headers,
        max_retries=max_retries,
        extract_items=lambda payload: list(payload),
    )
    commits = [_parse_commit(raw, username, since) for raw in raw_commits]
    return [commit for commit in commits if commit is not None]


async def _search_pull_requests(
    client: httpx2.AsyncClient,
    owner: str,
    repo: str,
    query: str,
    since: datetime,
    headers: Mapping[str, str],
    max_retries: int,
) -> list[PullRequestActivity]:
    url = f"{GITHUB_API_BASE}/search/issues"
    params = {
        "q": f"repo:{owner}/{repo} {query}",
        "per_page": str(_PER_PAGE),
        "page": "1",
    }
    raw_items = await _fetch_all_pages(
        client,
        url,
        params,
        headers=headers,
        max_retries=max_retries,
        extract_items=lambda payload: list(payload.get("items", [])),
    )
    prs = [_parse_pull_request(raw, owner, repo, since) for raw in raw_items]
    return [pr for pr in prs if pr is not None]


_RepoActivity = tuple[
    list[CommitActivity],
    list[PullRequestActivity],
    list[PullRequestActivity],
    list[PullRequestActivity],
]


async def _fetch_repo_activity(
    client: httpx2.AsyncClient,
    agent: Agent,
    repo: str,
    since: datetime,
    max_retries: int,
) -> _RepoActivity:
    owner, name = repo.split("/", 1)
    headers = _auth_headers(agent)
    username = agent.github_username

    commits = await _fetch_commits(client, owner, name, username, since, headers, max_retries)
    merged_prs = await _search_pull_requests(
        client, owner, name, f"type:pr author:{username} is:merged", since, headers, max_retries
    )
    open_prs = await _search_pull_requests(
        client, owner, name, f"type:pr author:{username} is:open", since, headers, max_retries
    )
    review_requests = await _search_pull_requests(
        client, owner, name, f"review-requested:{username} is:open", since, headers, max_retries
    )
    return commits, merged_prs, open_prs, review_requests


async def fetch_github_activity(
    agent: Agent,
    roles: dict[str, Role],
    since: datetime,
    client: httpx2.AsyncClient | None = None,
    max_retries: int = 3,
) -> GitHubActivity:
    """Fetch `agent`'s GitHub activity since `since`, across every repo in `agent.allowed_repos`.

    See the module docstring for the permission gate, endpoints, pagination, rate-limit handling,
    and merge-commit exclusion. A permission denial or a 404 for one repo becomes one
    `GitHubFetchError` in the result and does not stop the other repos; an exhausted rate limit
    raises `RateLimitError` for the whole call.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    else:
        since = since.astimezone(UTC)

    owns_client = client is None
    active_client = client if client is not None else _default_client()

    try:
        commits: list[CommitActivity] = []
        merged_prs: list[PullRequestActivity] = []
        open_prs: list[PullRequestActivity] = []
        review_requests: list[PullRequestActivity] = []
        errors: list[GitHubFetchError] = []

        for repo in agent.allowed_repos:
            try:
                require_permission(agent, "github:read", repo, roles)
            except PermissionDenied as exc:
                errors.append(GitHubFetchError(repo=repo, status=403, message=str(exc)))
                continue

            try:
                repo_commits, repo_merged, repo_open, repo_review = await _fetch_repo_activity(
                    active_client, agent, repo, since, max_retries
                )
            except _RepoFetchFailed as exc:
                errors.append(GitHubFetchError(repo=repo, status=exc.status, message=exc.message))
                continue

            commits.extend(repo_commits)
            merged_prs.extend(repo_merged)
            open_prs.extend(repo_open)
            review_requests.extend(repo_review)

        return GitHubActivity(
            commits=commits,
            merged_prs=merged_prs,
            open_prs=open_prs,
            review_requests=review_requests,
            errors=errors,
        )
    finally:
        if owns_client:
            await active_client.aclose()


__all__ = [
    "GITHUB_API_BASE",
    "CommitActivity",
    "PullRequestActivity",
    "GitHubFetchError",
    "GitHubActivity",
    "RateLimitError",
    "fetch_github_activity",
]
