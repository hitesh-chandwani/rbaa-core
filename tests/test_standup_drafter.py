"""Tests for `rbaa.standup` (#10): the standup drafter and claim validator.

Every test in the default suite is offline: `draft_standup`'s `client` parameter is always a fake
injected here, so the real `GeminiClient`/`google-genai` SDK is never touched and no network call
is ever made. `validate_claims` is plain, deterministic Python (NFR-01) and several tests call it
directly with a hand-built `WorkContext` and no client/model involved at all, per
`_docs/testing-guidelines.md` rule 1 (grounding is tested, not assumed) and #10's own acceptance
criteria ("test asserts this by calling the validator directly ... no model involved").

One `@pytest.mark.live` test makes a real Gemini call to confirm end-to-end wiring; it is skipped
by default and documents how to run it (see its docstring).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from rbaa.context import BlockerItem, CommitItem, PullRequestItem, TicketItem, WorkContext
from rbaa.roles import Role
from rbaa.standup import Claim, StandupScript, draft_standup, validate_claims

GENERATED_AT = datetime(2024, 1, 2, tzinfo=UTC)
SINCE = datetime(2024, 1, 1, tzinfo=UTC)
UPDATED = datetime(2024, 1, 1, 12, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------------------------


def _role(
    *,
    standup_max_seconds: int = 600,
    communication_style: str = "Direct and outcome-focused.",
    guidelines: str = "Only claim work you can back with a ticket, commit or merged PR.",
) -> Role:
    return Role(
        name="full_access",
        title="Senior Software Engineer",
        responsibilities=["Ship the roadmap"],
        communication_style=communication_style,
        standup_max_seconds=standup_max_seconds,
        default_voice="Kore",
        tool_permissions=["github:read", "jira:read"],
        guidelines=guidelines,
    )


def _commit(sha: str, title: str = "Fix the bug") -> CommitItem:
    return CommitItem(
        evidence_id=f"gh:commit:{sha}",
        id=sha,
        title=title,
        url=f"https://github.com/acme/repo/commit/{sha}",
        timestamp=UPDATED,
    )


def _pr(owner: str, repo: str, number: int, title: str = "Add feature") -> PullRequestItem:
    return PullRequestItem(
        evidence_id=f"gh:pr:{owner}/{repo}#{number}",
        id=str(number),
        title=title,
        url=f"https://github.com/{owner}/{repo}/pull/{number}",
        timestamp=UPDATED,
    )


def _ticket(key: str, *, status: str = "Done", status_category: str = "Done") -> TicketItem:
    return TicketItem(
        evidence_id=f"jira:{key}",
        key=key,
        summary=f"Work on {key}",
        status=status,
        status_category=status_category,
        updated=UPDATED,
        url=f"https://jira.example.com/browse/{key}",
    )


def _blocker(key: str, blocking_key: str | None = "BLK-1") -> BlockerItem:
    return BlockerItem(evidence_id=f"jira:{key}", key=key, blocking_key=blocking_key)


def _context(
    *,
    commits: list[CommitItem] | None = None,
    merged_prs: list[PullRequestItem] | None = None,
    open_reviews: list[PullRequestItem] | None = None,
    tickets: list[TicketItem] | None = None,
    blockers: list[BlockerItem] | None = None,
) -> WorkContext:
    return WorkContext(
        agent_id="bot",
        generated_at=GENERATED_AT,
        since=SINCE,
        commits=commits or [],
        merged_prs=merged_prs or [],
        open_reviews=open_reviews or [],
        tickets=tickets or [],
        blockers=blockers or [],
        errors=[],
        truncated={},
    )


class RecordingClient:
    """Fake `LLMClient`: returns a fixed raw response and records every call it receives."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def generate(self, prompt: str, *, model: str) -> str:
        self.calls.append({"prompt": prompt, "model": model})
        return self.response


class RaisingClient:
    """Fake `LLMClient` that fails the test if it is ever called."""

    async def generate(self, prompt: str, *, model: str) -> str:
        raise AssertionError("the model must not be called")


def _claims_json(claims: list[dict]) -> str:
    return json.dumps(claims)


# --------------------------------------------------------------------------------------------
# draft_standup: claims + evidence_ids in output, and role-styled script coverage
# --------------------------------------------------------------------------------------------


async def test_draft_standup_returns_claims_with_evidence_ids():
    context = _context(merged_prs=[_pr("acme", "repo", 1)])
    client = RecordingClient(
        _claims_json(
            [
                {
                    "text": "Shipped the feature",
                    "kind": "completed",
                    "evidence_ids": ["gh:pr:acme/repo#1"],
                }
            ]
        )
    )

    result = await draft_standup(_role(), context, client=client)

    assert isinstance(result, StandupScript)
    assert len(result.claims) == 1
    assert result.claims[0].evidence_ids == ["gh:pr:acme/repo#1"]


async def test_script_text_covers_completed_planned_and_blockers():
    context = _context(
        merged_prs=[_pr("acme", "repo", 1)],
        tickets=[_ticket("ABC-2", status="To Do", status_category="To Do")],
        blockers=[_blocker("ABC-3")],
    )
    client = RecordingClient(
        _claims_json(
            [
                {
                    "text": "shipped the feature",
                    "kind": "completed",
                    "evidence_ids": ["gh:pr:acme/repo#1"],
                },
                {
                    "text": "will pick up ABC-2 next",
                    "kind": "planned",
                    "evidence_ids": ["jira:ABC-2"],
                },
                {"text": "blocked on ABC-3", "kind": "blocker", "evidence_ids": ["jira:ABC-3"]},
            ]
        )
    )

    result = await draft_standup(_role(), context, client=client)

    assert "Completed:" in result.script_text
    assert "Planned:" in result.script_text
    assert "Blockers:" in result.script_text
    assert {c.kind for c in result.claims} == {"completed", "planned", "blocker"}


async def test_prompt_includes_role_communication_style_and_guidelines():
    role = _role(
        communication_style="UNIQUE_STYLE_MARKER_42",
        guidelines="UNIQUE_GUIDELINES_MARKER_77",
    )
    context = _context(commits=[_commit("abc")])
    client = RecordingClient(_claims_json([]))

    await draft_standup(role, context, client=client)

    assert len(client.calls) == 1
    prompt = client.calls[0]["prompt"]
    assert "UNIQUE_STYLE_MARKER_42" in prompt
    assert "UNIQUE_GUIDELINES_MARKER_77" in prompt


# --------------------------------------------------------------------------------------------
# validate_claims: pure Python, no model involved (rules a and b)
# --------------------------------------------------------------------------------------------


def test_validate_claims_drops_claim_citing_nonexistent_evidence_id():
    context = _context(commits=[_commit("abc")])
    claims = [Claim(text="fabricated", kind="completed", evidence_ids=["gh:commit:doesnotexist"])]

    assert validate_claims(claims, context) == []


def test_validate_claims_drops_claim_with_no_evidence_ids():
    context = _context(commits=[_commit("abc")])
    claims = [Claim(text="unsupported", kind="planned", evidence_ids=[])]

    assert validate_claims(claims, context) == []


def test_validate_claims_drops_claim_with_one_invalid_id_among_valid_ones():
    context = _context(commits=[_commit("abc")])
    claims = [
        Claim(text="half right", kind="planned", evidence_ids=["gh:commit:abc", "gh:commit:fake"])
    ]

    assert validate_claims(claims, context) == []


def test_validate_claims_keeps_completed_claim_grounded_by_merged_pr():
    context = _context(merged_prs=[_pr("acme", "repo", 1)])
    claims = [Claim(text="shipped it", kind="completed", evidence_ids=["gh:pr:acme/repo#1"])]

    assert validate_claims(claims, context) == claims


def test_validate_claims_keeps_completed_claim_grounded_by_commit():
    context = _context(commits=[_commit("abc")])
    claims = [Claim(text="shipped it", kind="completed", evidence_ids=["gh:commit:abc"])]

    assert validate_claims(claims, context) == claims


def test_validate_claims_keeps_completed_claim_grounded_by_done_ticket():
    context = _context(tickets=[_ticket("ABC-1", status="Done", status_category="Done")])
    claims = [Claim(text="finished it", kind="completed", evidence_ids=["jira:ABC-1"])]

    assert validate_claims(claims, context) == claims


def test_validate_claims_drops_completed_claim_citing_open_pr_only():
    context = _context(open_reviews=[_pr("acme", "repo", 9)])
    claims = [Claim(text="fabricated", kind="completed", evidence_ids=["gh:pr:acme/repo#9"])]

    assert validate_claims(claims, context) == []


def test_validate_claims_drops_completed_claim_citing_non_done_ticket_only():
    context = _context(
        tickets=[_ticket("ABC-5", status="In Progress", status_category="In Progress")]
    )
    claims = [Claim(text="fabricated", kind="completed", evidence_ids=["jira:ABC-5"])]

    assert validate_claims(claims, context) == []


def test_validate_claims_keeps_planned_claim_citing_non_done_ticket():
    """Rule (b) only restricts `kind == "completed"`; a planned claim about an open ticket is
    grounded by rule (a) alone (the ticket exists in `context.tickets`)."""
    context = _context(
        tickets=[_ticket("ABC-5", status="In Progress", status_category="In Progress")]
    )
    claims = [Claim(text="will pick this up", kind="planned", evidence_ids=["jira:ABC-5"])]

    assert validate_claims(claims, context) == claims


def test_validate_claims_drops_blocker_claim_citing_merged_pr_only():
    """QA's exact FAIL-reproducing scenario: a `WorkContext` with one merged PR and empty
    `blockers`, and a `kind="blocker"` claim citing that PR's evidence_id. Rule (a) alone would
    pass this (the PR exists in the context); rule (c) requires the evidence to resolve into
    `context.blockers` specifically, so this must be dropped."""
    context = _context(merged_prs=[_pr("acme", "repo", 123)], blockers=[])
    claims = [Claim(text="waiting on review", kind="blocker", evidence_ids=["gh:pr:acme/repo#123"])]

    assert validate_claims(claims, context) == []


def test_validate_claims_keeps_blocker_claim_grounded_by_context_blockers():
    """Positive case for rule (c): a blocker claim whose evidence_id IS in `context.blockers`
    passes validation."""
    context = _context(blockers=[_blocker("ABC-3")])
    claims = [Claim(text="blocked on ABC-3", kind="blocker", evidence_ids=["jira:ABC-3"])]

    assert validate_claims(claims, context) == claims


# --------------------------------------------------------------------------------------------
# Grounding via the full draft_standup path (testing-guidelines rule 1)
# --------------------------------------------------------------------------------------------


async def test_fabricated_completed_claim_absent_from_claims_and_script_text():
    context = _context(commits=[_commit("abc", title="Real fix")])
    client = RecordingClient(
        _claims_json(
            [
                {
                    "text": "fixed the real bug",
                    "kind": "completed",
                    "evidence_ids": ["gh:commit:abc"],
                },
                {
                    "text": "shipped the payment refactor",
                    "kind": "completed",
                    "evidence_ids": ["gh:commit:nonexistent"],
                },
            ]
        )
    )

    result = await draft_standup(_role(), context, client=client)

    assert "fixed the real bug" in result.script_text
    assert "shipped the payment refactor" not in result.script_text
    assert {c.text for c in result.claims} == {"fixed the real bug"}


async def test_claim_with_no_evidence_ids_never_reaches_script_text():
    context = _context(commits=[_commit("abc")])
    client = RecordingClient(
        _claims_json([{"text": "unsupported claim", "kind": "planned", "evidence_ids": []}])
    )

    result = await draft_standup(_role(), context, client=client)

    assert "unsupported claim" not in result.script_text
    assert result.claims == []


async def test_blocker_claim_grounded_by_context_blockers_is_kept():
    context = _context(blockers=[_blocker("ABC-3")])
    client = RecordingClient(
        _claims_json(
            [{"text": "blocked on ABC-3", "kind": "blocker", "evidence_ids": ["jira:ABC-3"]}]
        )
    )

    result = await draft_standup(_role(), context, client=client)

    assert "blocked on ABC-3" in result.script_text
    assert result.claims[0].kind == "blocker"


async def test_blocker_claim_with_fabricated_evidence_is_dropped():
    context = _context(blockers=[_blocker("ABC-3")])
    client = RecordingClient(
        _claims_json(
            [
                {
                    "text": "blocked on something imaginary",
                    "kind": "blocker",
                    "evidence_ids": ["jira:ZZZ-99"],
                }
            ]
        )
    )

    result = await draft_standup(_role(), context, client=client)

    assert "blocked on something imaginary" not in result.script_text
    assert result.claims == []


async def test_blocker_claim_citing_unrelated_merged_pr_is_dropped():
    """QA's exact FAIL-reproducing scenario, exercised through the full `draft_standup` path: a
    `WorkContext` with one merged PR and empty `blockers`, and a `kind="blocker"` claim citing
    that PR's evidence_id. Rule (a) alone (existence anywhere in the context) would let this
    through; rule (c) requires the evidence to resolve into `context.blockers` specifically, so
    the claim must not reach `script_text`."""
    context = _context(merged_prs=[_pr("acme", "repo", 123)], blockers=[])
    client = RecordingClient(
        _claims_json(
            [
                {
                    "text": "waiting on review",
                    "kind": "blocker",
                    "evidence_ids": ["gh:pr:acme/repo#123"],
                }
            ]
        )
    )

    result = await draft_standup(_role(), context, client=client)

    assert "waiting on review" not in result.script_text
    assert "Blockers:" not in result.script_text
    assert result.claims == []


# --------------------------------------------------------------------------------------------
# Speaking-time formula and trim priority (blocker > completed > planned)
# --------------------------------------------------------------------------------------------


async def test_speaking_time_formula_is_respected_when_no_trim_needed():
    context = _context(commits=[_commit("abc")])
    client = RecordingClient(
        _claims_json(
            [{"text": "fixed a small bug", "kind": "completed", "evidence_ids": ["gh:commit:abc"]}]
        )
    )

    result = await draft_standup(_role(standup_max_seconds=600), context, client=client)

    word_count = len(result.script_text.split())
    estimated_seconds = word_count / 150 * 60
    assert estimated_seconds <= 600


async def test_trim_drops_lowest_priority_claims_first_until_under_budget():
    """Fixed-length fake claims force a trim: with a 9-second budget, the full set (25 words,
    10s) must shed the lowest-priority tier first (planned), and within that tier the
    last-proposed claim first (judgment call 5 in `rbaa.standup`'s module docstring) -- leaving
    exactly [blocker, completed, planned_A] and dropping planned_B."""
    context = _context(
        merged_prs=[_pr("acme", "repo", 1)],
        tickets=[
            _ticket("ABC-1", status="To Do", status_category="To Do"),
            _ticket("ABC-2", status="To Do", status_category="To Do"),
        ],
        blockers=[_blocker("ABC-3")],
    )
    proposed = [
        {
            "text": "Shipped the payment refactor today",  # 5 words
            "kind": "completed",
            "evidence_ids": ["gh:pr:acme/repo#1"],
        },
        {
            "text": "Plan to add retries tomorrow",  # 5 words
            "kind": "planned",
            "evidence_ids": ["jira:ABC-1"],
        },
        {
            "text": "Plan to write more tests next week",  # 7 words
            "kind": "planned",
            "evidence_ids": ["jira:ABC-2"],
        },
        {
            "text": "Blocked by missing credentials access",  # 5 words
            "kind": "blocker",
            "evidence_ids": ["jira:ABC-3"],
        },
    ]
    client = RecordingClient(_claims_json(proposed))

    result = await draft_standup(_role(standup_max_seconds=9), context, client=client)

    kept_texts = {c.text for c in result.claims}
    assert kept_texts == {
        "Shipped the payment refactor today",
        "Plan to add retries tomorrow",
        "Blocked by missing credentials access",
    }
    assert "Plan to write more tests next week" not in result.script_text

    word_count = len(result.script_text.split())
    estimated_seconds = word_count / 150 * 60
    assert estimated_seconds <= 9


# --------------------------------------------------------------------------------------------
# Empty context
# --------------------------------------------------------------------------------------------


async def test_empty_context_yields_no_activity_signal_and_no_model_call():
    context = _context()

    result = await draft_standup(_role(), context, client=RaisingClient())

    assert "no recorded activity" in result.script_text.lower()
    assert result.claims == []


# --------------------------------------------------------------------------------------------
# Model ID: configurable via env var, overridable via the `model` parameter
# --------------------------------------------------------------------------------------------


async def test_model_id_read_from_env_var_when_not_passed(monkeypatch):
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "env-configured-model")
    context = _context(commits=[_commit("abc")])
    client = RecordingClient(_claims_json([]))

    await draft_standup(_role(), context, client=client)

    assert client.calls[0]["model"] == "env-configured-model"


async def test_model_param_overrides_env_var(monkeypatch):
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "env-configured-model")
    context = _context(commits=[_commit("abc")])
    client = RecordingClient(_claims_json([]))

    await draft_standup(_role(), context, model="explicit-model", client=client)

    assert client.calls[0]["model"] == "explicit-model"


# --------------------------------------------------------------------------------------------
# Live: one real call to confirm end-to-end wiring (skipped by default)
# --------------------------------------------------------------------------------------------


@pytest.mark.live
async def test_live_draft_standup_end_to_end():
    """Makes one real Gemini call. Skipped unless `GOOGLE_API_KEY` is set.

    Run it with:

        GOOGLE_API_KEY=... uv run pytest -m live \
            tests/test_standup_drafter.py::test_live_draft_standup_end_to_end

    Optionally set `GEMINI_TEXT_MODEL` to pick a specific model; otherwise the code default is
    used. This only confirms the real `GeminiClient`/SDK wiring does not raise and returns a
    `StandupScript` -- it does not assert anything about the model's exact wording.
    """
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY is not set")

    context = _context(commits=[_commit("abc123", title="Fix the flaky retry logic")])

    result = await draft_standup(_role(), context)

    assert isinstance(result, StandupScript)
    assert isinstance(result.script_text, str)
    assert result.script_text != ""
