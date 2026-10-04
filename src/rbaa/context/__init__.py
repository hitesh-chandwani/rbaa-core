"""The Work Context Builder (#9): a single deterministic JSON payload with evidence IDs.

`build_work_context(agent, roles, since=None, ...)` merges one agent's GitHub activity (#7,
`fetch_github_activity` -> `GitHubActivity`) and Jira activity (#8, `fetch_jira_activity` ->
`JiraActivity`) into one `WorkContext` pydantic model (FR-03, NFR-01): every fact in it carries an
`evidence_id` so a consumer can trace any claim back to its source.

Field mapping (pinned by #9; see the issue for the full rationale)
--------------------------------------------------------------------

- `commits` <- `GitHubActivity.commits`
- `merged_prs` <- `GitHubActivity.merged_prs`
- `open_reviews` <- `GitHubActivity.review_requests` (renamed: PRs awaiting *this* agent's review)
- `tickets` <- `JiraActivity.tickets`
- `blockers` <- `JiraActivity.blockers`
- `errors` <- union of `GitHubActivity.errors` and `JiraActivity.errors`, each tagged `source`
- `truncated` <- per-category dropped-item counts (every category, always present)

`GitHubActivity.open_prs` and `JiraActivity.completed`/`status_changes` have no top-level field
per the acceptance criteria. `completed`/`status_changes` fold into each ticket's existing
`status`/`status_category`. `open_prs` is dropped from the payload entirely, cross-linking
included -- see judgment call 5 below.

Evidence ID formats (exact, pinned by #9)
-------------------------------------------

- Commits: `gh:commit:{sha}`
- PRs (merged or open-review): `gh:pr:{owner}/{repo}#{number}`, parsed from `PullRequestActivity.
  url` (the GitHub PR URL) since `.id` alone is not owner/repo-qualified.
- Tickets: `jira:{key}`
- Blockers: `jira:{key}` of the *blocked* ticket, never `blocking_key`.

Determinism
------------

Same inputs + injected `clock` -> byte-identical `json.dumps(model.model_dump(mode="json"),
sort_keys=True)`. Every category list is sorted by `evidence_id` ascending as the final step,
after any per-category cap is applied (see below) -- picked over timestamp ordering because
`evidence_id` is already unique and stable, so it needs no tie-breaker.

`since` default logic (precise, pinned by #9)
------------------------------------------------

1. If the caller passes `since` explicitly, it is used as-is (judgment call 1 below).
2. Else, if `last_standup_at` is given, `since = last_standup_at`.
3. Else, `since` = 00:00:00 of the previous business day in `timezone` (an IANA name), computed
   from `clock()`. Monday -> previous Friday; a clock landing on Saturday/Sunday also rolls back
   to the preceding Friday. This issue does not add standup-time persistence: `last_standup_at` is
   accepted as a plain parameter, deferring storage/lookup to #18/#19.

Partial failure
-----------------

`github_fetch`/`jira_fetch` are called independently. If one of them *raises* (the way
`RateLimitError` propagates "for the whole call" in #7/#8, rather than becoming a
`GitHubFetchError`/`JiraFetchError`), that source's data is treated as empty and one synthesized
`errors` entry is added for it (judgment calls 2 and 3 below). If *both* raise, `ContextUnavailable`
is raised instead of returning a payload. A fetch call that *succeeds* but carries its own
per-repo/per-project `GitHubFetchError`/`JiraFetchError` entries is not a "source failing" in this
sense -- those are unioned into `errors` as before, tagged by source, exactly as #7/#8 already
designed them to be non-fatal.

No credentials
----------------

Only `id`/`title`/`url`/`timestamp`-shaped fields (and Jira's `key`/`summary`/`status`/...) ever
reach `WorkContext`. Nothing here ever reads `agent.github_token`, `agent.jira_token`, or any
header value, so none of it can appear in the serialized JSON.

Judgment calls (#9 did not pin these; disclosed per the engineer's issue comment)
------------------------------------------------------------------------------------

1. **Explicit `since` precedence.** The issue's "since default logic" section only describes the
   choice between `last_standup_at` and the computed previous-business-day default; it does not
   say what an explicitly-passed `since` argument does relative to `last_standup_at`. The most
   natural reading of a parameter literally named `since` is that passing it overrides the whole
   default chain, so that is what this implementation does: `since` (if given) > `last_standup_at`
   (if given) > computed default.
2. **What "a source failing" means.** Taken to mean the injected `github_fetch`/`jira_fetch`
   *raised* for the whole call (e.g. `RateLimitError`), not that the returned `GitHubActivity`/
   `JiraActivity` happens to carry some per-repo/per-project errors of its own -- those are already
   non-fatal by #7/#8's own design and are simply unioned into `errors`.
3. **Shape of a synthesized whole-source error.** A whole-call exception has no repo/project to
   scope it to, so its `ContextFetchError` entry uses `status=503` and `resource=None`.
4. **Merged error field name.** `GitHubFetchError.repo` and `JiraFetchError.project` are unified
   into one field, `resource`, on `ContextFetchError`, rather than keeping two mostly-null fields.
5. **`GitHubActivity.open_prs` is dropped entirely**, cross-linking included. The field-mapping
   section forbids inventing a new top-level key for it, and it has no "sibling list" slot to fold
   into without doing so, so only `merged_prs` and `open_reviews` (the two PR categories actually
   in the payload) participate in cross-linking.
6. **Cross-linking happens before per-category truncation.** A kept item's `related` can reference
   an evidence ID that was independently capped out of its own category. The issue does not ask for
   `related` to be recomputed after capping, and doing so would make it depend on cap order in a
   way the issue does not specify.
7. **`related` lists are sorted** for the same determinism reason the top-level category lists are,
   even though the issue's determinism section only names the latter explicitly.
8. **A blocker with no matching ticket** (should not happen per #8's own emission logic -- every
   blocker is derived from an issue that is also always emitted as a ticket) sorts as least-recent
   for capping purposes (`datetime.min`, UTC) rather than raising.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from rbaa.agents import Agent
from rbaa.integrations.github import (
    CommitActivity,
    GitHubActivity,
    PullRequestActivity,
    fetch_github_activity,
)
from rbaa.integrations.jira import (
    JiraActivity,
    JiraBlocker,
    JiraTicket,
    fetch_jira_activity,
)
from rbaa.roles import Role

_TICKET_KEY_RE = re.compile(r"[A-Z]+-\d+")
_PR_URL_RE = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)")

# Judgment call 3 (see module docstring): the status used for a synthesized whole-source error,
# since a raised exception has no HTTP status of its own to carry.
_SOURCE_FAILURE_STATUS = 503

_MIN_TIMESTAMP = datetime.min.replace(tzinfo=UTC)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class CommitItem(BaseModel):
    """One commit, with its evidence ID added."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    id: str
    title: str
    url: str
    timestamp: datetime


class PullRequestItem(BaseModel):
    """One pull request (merged, or awaiting the agent's review), with its evidence ID added."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    id: str
    title: str
    url: str
    timestamp: datetime
    related: list[str] = Field(default_factory=list)


class TicketItem(BaseModel):
    """One Jira ticket, with its evidence ID added."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    key: str
    summary: str
    status: str
    status_category: str
    updated: datetime
    url: str
    related: list[str] = Field(default_factory=list)


class BlockerItem(BaseModel):
    """One blocked ticket, with its evidence ID (of the *blocked* ticket) added."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    key: str
    blocking_key: str | None = None


class ContextFetchError(BaseModel):
    """One source's fetch error, tagged by origin.

    `resource` is the repo (GitHub) or project key (Jira) the error is scoped to, or `None` for a
    synthesized whole-source failure (judgment calls 3 and 4 in the module docstring).
    """

    model_config = ConfigDict(frozen=True)

    source: str
    status: int
    message: str
    resource: str | None = None


class WorkContext(BaseModel):
    """One agent's merged GitHub + Jira activity, as a single deterministic, evidence-backed
    payload. See the module docstring for the field mapping, evidence ID formats, and
    determinism rule."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    generated_at: datetime
    since: datetime
    commits: list[CommitItem]
    merged_prs: list[PullRequestItem]
    open_reviews: list[PullRequestItem]
    tickets: list[TicketItem]
    blockers: list[BlockerItem]
    errors: list[ContextFetchError]
    truncated: dict[str, int]


class ContextUnavailable(Exception):
    """Both the GitHub and Jira activity fetches failed; no `WorkContext` could be built.

    Carries both underlying exceptions for diagnostics. Neither exception raised by #7/#8's own
    fetchers (`RateLimitError`, or any other) ever carries a credential, so this stays consistent
    with "no credentials" -- but this exception itself is still never put in the payload.
    """

    def __init__(self, github_error: Exception, jira_error: Exception) -> None:
        self.github_error = github_error
        self.jira_error = jira_error
        super().__init__(
            "both GitHub and Jira activity fetches failed: "
            f"github={github_error!r}, jira={jira_error!r}"
        )


def _aware_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; pass an aware one through unchanged (mirrors #7/#8)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _previous_business_day_start(now: datetime, timezone: str) -> datetime:
    """Start (00:00:00) of the previous business day (Mon-Fri) in `timezone`, from `now`.

    Monday rolls back to the preceding Friday; a `now` landing on Saturday or Sunday also rolls
    back to the preceding Friday.
    """
    tz = ZoneInfo(timezone)
    local_now = _aware_utc(now).astimezone(tz)
    candidate = local_now.date() - timedelta(days=1)
    while candidate.weekday() >= 5:  # Saturday=5, Sunday=6
        candidate -= timedelta(days=1)
    return datetime(candidate.year, candidate.month, candidate.day, tzinfo=tz)


def _resolve_since(
    since: datetime | None,
    last_standup_at: datetime | None,
    timezone: str,
    now: datetime,
) -> datetime:
    """Judgment call 1 (see module docstring): explicit `since` > `last_standup_at` > default."""
    if since is not None:
        resolved = since
    elif last_standup_at is not None:
        resolved = last_standup_at
    else:
        resolved = _previous_business_day_start(now, timezone)
    return _aware_utc(resolved)


def _pr_evidence_id(url: str) -> str:
    match = _PR_URL_RE.match(url)
    if not match:
        raise ValueError(f"cannot parse owner/repo/number from PR url: {url!r}")
    owner, repo, number = match.groups()
    return f"gh:pr:{owner}/{repo}#{number}"


def _build_commit_items(commits: list[CommitActivity]) -> list[CommitItem]:
    return [
        CommitItem(
            evidence_id=f"gh:commit:{c.id}",
            id=c.id,
            title=c.title,
            url=c.url,
            timestamp=c.timestamp,
        )
        for c in commits
    ]


def _build_pr_items(prs: list[PullRequestActivity]) -> list[PullRequestItem]:
    return [
        PullRequestItem(
            evidence_id=_pr_evidence_id(pr.url),
            id=pr.id,
            title=pr.title,
            url=pr.url,
            timestamp=pr.timestamp,
        )
        for pr in prs
    ]


def _build_ticket_items(tickets: list[JiraTicket]) -> list[TicketItem]:
    return [
        TicketItem(
            evidence_id=f"jira:{t.key}",
            key=t.key,
            summary=t.summary,
            status=t.status,
            status_category=t.status_category,
            updated=t.updated,
            url=t.url,
        )
        for t in tickets
    ]


def _build_blocker_items(blockers: list[JiraBlocker]) -> list[BlockerItem]:
    return [
        BlockerItem(evidence_id=f"jira:{b.key}", key=b.key, blocking_key=b.blocking_key)
        for b in blockers
    ]


def _cross_link(pr_items: list[PullRequestItem], ticket_items: list[TicketItem]) -> None:
    """Mutate `related` in place on both sides (judgment calls 5, 6 and 7 in the module
    docstring): a PR whose title contains a ticket key already present in `ticket_items` links to
    that ticket, and vice versa. `related` is sorted on both sides for determinism."""
    tickets_by_key = {t.key: t for t in ticket_items}
    for pr in pr_items:
        matched_keys = {key for key in _TICKET_KEY_RE.findall(pr.title) if key in tickets_by_key}
        for key in matched_keys:
            ticket = tickets_by_key[key]
            if ticket.evidence_id not in pr.related:
                pr.related.append(ticket.evidence_id)
            if pr.evidence_id not in ticket.related:
                ticket.related.append(pr.evidence_id)
    for pr in pr_items:
        pr.related.sort()
    for ticket in ticket_items:
        ticket.related.sort()


def _finalize_category(
    items: list,
    timestamp_of: Callable[[object], datetime],
    max_per_category: int,
) -> tuple[list, int]:
    """Cap at `max_per_category`, keeping the most recent by `timestamp_of`, then re-sort the kept
    subset by `evidence_id` ascending (the Determinism rule)."""
    if len(items) > max_per_category:
        ordered = sorted(items, key=timestamp_of, reverse=True)
        kept = ordered[:max_per_category]
        dropped = len(items) - max_per_category
    else:
        kept = list(items)
        dropped = 0
    kept.sort(key=lambda item: item.evidence_id)
    return kept, dropped


async def build_work_context(
    agent: Agent,
    roles: dict[str, Role],
    since: datetime | None = None,
    *,
    last_standup_at: datetime | None = None,
    timezone: str = "UTC",
    max_per_category: int = 20,
    clock: Callable[[], datetime] | None = None,
    github_fetch: Callable[..., Awaitable[GitHubActivity]] = fetch_github_activity,
    jira_fetch: Callable[..., Awaitable[JiraActivity]] = fetch_jira_activity,
) -> WorkContext:
    """Build `agent`'s `WorkContext`: merged GitHub + Jira activity with evidence IDs.

    See the module docstring for the field mapping, evidence ID formats, determinism rule, the
    `since` default chain, partial-failure semantics, and cross-linking. Raises
    `ContextUnavailable` only when *both* `github_fetch` and `jira_fetch` raise.
    """
    now = clock() if clock is not None else _default_clock()
    now = _aware_utc(now)
    resolved_since = _resolve_since(since, last_standup_at, timezone, now)

    github_activity: GitHubActivity | None = None
    jira_activity: JiraActivity | None = None
    github_exc: Exception | None = None
    jira_exc: Exception | None = None

    try:
        github_activity = await github_fetch(agent, roles, resolved_since)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see judgment call 2
        github_exc = exc

    try:
        jira_activity = await jira_fetch(agent, roles, resolved_since)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see judgment call 2
        jira_exc = exc

    if github_exc is not None and jira_exc is not None:
        raise ContextUnavailable(github_exc, jira_exc)

    errors: list[ContextFetchError] = []

    if github_exc is not None:
        errors.append(
            ContextFetchError(
                source="github",
                status=_SOURCE_FAILURE_STATUS,
                message=str(github_exc),
                resource=None,
            )
        )
        github_activity = GitHubActivity()
    else:
        assert github_activity is not None  # for type-checkers: no exception means a result
        errors.extend(
            ContextFetchError(source="github", status=e.status, message=e.message, resource=e.repo)
            for e in github_activity.errors
        )

    if jira_exc is not None:
        errors.append(
            ContextFetchError(
                source="jira",
                status=_SOURCE_FAILURE_STATUS,
                message=str(jira_exc),
                resource=None,
            )
        )
        jira_activity = JiraActivity()
    else:
        assert jira_activity is not None  # for type-checkers: no exception means a result
        errors.extend(
            ContextFetchError(source="jira", status=e.status, message=e.message, resource=e.project)
            for e in jira_activity.errors
        )

    commit_items = _build_commit_items(github_activity.commits)
    merged_pr_items = _build_pr_items(github_activity.merged_prs)
    open_review_items = _build_pr_items(github_activity.review_requests)
    ticket_items = _build_ticket_items(jira_activity.tickets)
    blocker_items = _build_blocker_items(jira_activity.blockers)

    _cross_link(merged_pr_items + open_review_items, ticket_items)

    # Judgment call 8 (see module docstring): a blocker whose key is missing from this fetch's own
    # tickets sorts as least-recent rather than raising.
    ticket_updated_by_key = {t.key: t.updated for t in ticket_items}

    truncated: dict[str, int] = {}
    commit_items, truncated["commits"] = _finalize_category(
        commit_items, lambda c: c.timestamp, max_per_category
    )
    merged_pr_items, truncated["merged_prs"] = _finalize_category(
        merged_pr_items, lambda p: p.timestamp, max_per_category
    )
    open_review_items, truncated["open_reviews"] = _finalize_category(
        open_review_items, lambda p: p.timestamp, max_per_category
    )
    ticket_items, truncated["tickets"] = _finalize_category(
        ticket_items, lambda t: t.updated, max_per_category
    )
    blocker_items, truncated["blockers"] = _finalize_category(
        blocker_items,
        lambda b: ticket_updated_by_key.get(b.key, _MIN_TIMESTAMP),
        max_per_category,
    )

    return WorkContext(
        agent_id=agent.agent_id,
        generated_at=now,
        since=resolved_since,
        commits=commit_items,
        merged_prs=merged_pr_items,
        open_reviews=open_review_items,
        tickets=ticket_items,
        blockers=blocker_items,
        errors=errors,
        truncated=truncated,
    )


__all__ = [
    "CommitItem",
    "PullRequestItem",
    "TicketItem",
    "BlockerItem",
    "ContextFetchError",
    "WorkContext",
    "ContextUnavailable",
    "build_work_context",
]
