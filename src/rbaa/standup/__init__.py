"""The standup drafter and claim validator (#10): draft (LLM) + validate (deterministic Python).

`draft_standup(role, context, *, model=None, client=None) -> StandupScript` produces a
spoken-length standup update from a `Role` (#4) and a `WorkContext` (#9). The architecture is
pinned by #10 as two separate stages:

1. **Draft (LLM, non-deterministic).** The model is given the role and the context as JSON and
   returns candidate `Claim`s: `text`, `kind` (`"completed"`, `"planned"` or `"blocker"`) and the
   `evidence_ids` it believes support `text`. The LLM only *proposes* -- nothing it says is trusted
   as fact yet.
2. **Validate (plain Python, deterministic, no model call).** `validate_claims(claims, context)`
   checks every claim's `evidence_ids` against `context` and drops the ones that fail, before
   anything reaches `script_text` (NFR-01). This is the only code allowed to decide what is
   grounded; the LLM's own wording is never trusted for that decision.

A claim that fails validation is dropped entirely for this call -- it is not sent back to the
model to retry (#10 "out of scope", kept narrow deliberately).

Validation rules (pinned by #10, using #9's exact evidence_id formats: ``gh:commit:*``,
``gh:pr:*``, ``jira:*``)
---------------------------------------------------------------------------------------------------

- **Rule (a) -- existence.** An `evidence_id` is valid only if it literally appears in
  `context.commits`, `context.merged_prs`, `context.tickets`, or `context.blockers`.
  `context.open_reviews` is deliberately *not* part of this universe (judgment call 1 below): a
  claim citing only an open-review PR's evidence_id fails here, for any `kind`, not only
  `"completed"`.
- **Rule (b) -- completed-work grounding.** A claim with `kind == "completed"` is valid only if
  *at least one* of its evidence_ids resolves to `context.merged_prs`, `context.commits`, or a
  `context.tickets` entry with `status_category == "Done"`. An evidence_id that only resolves to
  an open PR or a non-Done ticket does not satisfy this rule.
- **Rule (c) -- blocker grounding.** A claim with `kind == "blocker"` is valid only if *at least
  one* of its evidence_ids resolves to an item actually present in `context.blockers` (matched by
  that blocker's own `evidence_id`, the `jira:{key}` of the *blocked* ticket per #9). An
  evidence_id that only resolves elsewhere (a merged PR, a commit, or a ticket that is not in
  `context.blockers`) does not satisfy this rule, so such a claim is dropped even though it passes
  rule (a). This closes the gap QA found in judgment call 4 below -- the groomed issue's acceptance
  criteria already required "blockers spoken are only those present in `context.blockers`"; this
  was simply not implemented until now.
- **All-or-nothing per claim.** If *any* of a claim's evidence_ids fails rule (a), the whole claim
  is dropped, not just that one id (judgment call 2 below).
- **No evidence_ids at all fails rule (a).** A claim with an empty `evidence_ids` list is dropped
  (judgment call 3 below) -- this is required by the issue's own grounding test criterion ("a
  claim injected with no evidence_ids ... never appears in script_text").
- `"planned"` claims have no rule beyond (a); the issue pins only rules (a) and (b) for them.

Speaking time and trimming (pinned by #10)
---------------------------------------------

``estimated_seconds = word_count(script_text) / 150 * 60`` (see `_WORDS_PER_MINUTE`).
`draft_standup` guarantees `estimated_seconds <= role.standup_max_seconds` by trimming whole
claims (never partial text) in this exact priority when over budget: keep all `kind == "blocker"`
claims first, then `kind == "completed"`, then `kind == "planned"`, dropping lowest-priority
claims first. Within the lowest surviving priority tier, claims are dropped from the end of that
tier's list first (judgment call 5 below) -- the issue pins the cross-tier order but not the
within-tier order.

Empty context
----------------

When `context` has no commits, merged PRs, open reviews, tickets or blockers at all, the model is
never called (judgment call 6 below): `draft_standup` returns a canned "no recorded activity"
`script_text` and an empty `claims` list directly. The same canned text is used as a fallback
whenever validation (plus trimming) leaves zero claims for a *non-empty* context too, so
`script_text` is never empty (judgment call 7 below).

Model selection
------------------

The Gemini model id is resolved as: the `model` parameter (if given) > the `GEMINI_TEXT_MODEL`
environment variable (if set) > `DEFAULT_GEMINI_TEXT_MODEL` in code. `_docs/design.md`'s "Gemini
3.8 Flash" was flagged there as unverified ("confirm exact model ID against the API before
coding"); `gemini-3.8-flash` was confirmed against https://ai.google.dev/gemini-api/docs/models on
2026-10-04 as a current, real Gemini Flash model id, so it is used as the code default.

The LLM client sits behind the `LLMClient` protocol (`async def generate(self, prompt, *, model)
-> str`) so tests never construct a real Gemini client. The default, real implementation
(`GeminiClient`) wraps the `google-genai` SDK and is constructed lazily, only when `client` is not
injected and there is actually something to draft -- so no test that injects a fake `client`, and
no empty-context call, ever imports or touches `google.genai`.

Judgment calls (#10 did not pin these; disclosed per the engineer's issue comment)
--------------------------------------------------------------------------------------

1. **`open_reviews` excluded from the rule (a) universe for every claim kind**, not only
   `"completed"`. The issue's rule (a) lists exactly four fields (`commits`, `merged_prs`,
   `tickets`, `blockers`); `open_reviews` is not one of them. This also matches `open_reviews`'
   own meaning in #9 (PRs awaiting *this* agent's review of someone else's work, not evidence of
   this agent's own work).
2. **A claim with one bad evidence_id and one good one is dropped entirely**, not partially kept.
   "A claim citing an evidence_id not present anywhere in the context is dropped entirely" reads
   most naturally as "any invalid id taints the whole claim", since partial trust would defeat the
   purpose of grounding.
3. **An empty `evidence_ids` list fails rule (a)** for every claim, including `"planned"` and
   `"blocker"` ones. A real, well-formed `"planned"` claim about an existing (not-yet-Done) ticket
   still has a valid evidence_id (the ticket itself, via rule a) to cite, so this does not stop
   genuine forward-looking claims; it only stops ones with literally nothing behind them.
4. **Resolved -- rule (c) added for `"blocker"` claims.** Originally this implementation had no
   rule beyond (a) for `kind == "blocker"` claims, reasoning that the issue pinned only rules (a)
   and (b). QA FAILed this: the groomed issue's acceptance criteria already state, verbatim,
   "blockers spoken are only those present in `context.blockers`" -- that is not satisfiable by
   rule (a) alone, since rule (a)'s existence universe also includes commits, merged_prs and
   tickets. QA proved it with a `WorkContext` holding one merged PR and empty `blockers`, and a
   `kind="blocker"` claim citing that PR's evidence_id, which incorrectly passed. Rule (c) above
   fixes this by requiring a blocker claim's evidence to resolve into `context.blockers`
   specifically, mirroring rule (b)'s structure for completed claims.
5. **Within-tier trim order.** The issue pins cross-tier drop order (planned before completed
   before blocker) but not which claim within a tier goes first. This drops from the end of the
   tier's list (last-proposed-first within a tier), simply because it is the simplest deterministic
   rule and is spelled out here for the trim test to pin down exactly.
6. **The model is never called for a fully empty context.** The issue says only that `script_text`
   must contain a "no recorded activity" signal and `claims` must be empty; it does not say whether
   the model is still asked to draft something. Skipping the call entirely is strictly safer (zero
   chance of a fabricated claim slipping through) and makes the "no claim is fabricated" test
   airtight by construction rather than by trusting validation to catch everything.
7. **The same "no recorded activity" fallback is used whenever final `claims` is empty**, even for
   a non-empty context where every proposed claim failed validation, so `script_text` can never end
   up blank. The issue only requires this for the literally-empty-context case; extending it avoids
   an unspecified edge case producing empty spoken text.
8. **Presentation order in `script_text` is "Completed, Planned, Blockers"**, matching the order
   the acceptance criteria lists them in ("covers what was done, what is planned, and blockers").
   This is independent of the trim *priority* order (blockers > completed > planned), which is
   about what survives a cut, not the order claims are spoken in.
9. **LLM response contract.** The issue says the model "returns a list of candidate claims" but
   does not pin the wire format. This implementation expects the raw text response to be a JSON
   array of objects (optionally wrapped as `{"claims": [...]}`), each shaped like `Claim`, and
   tolerates a ```` ```json ```` fence around it. A response that fails to parse yields zero
   candidate claims (so validation and the empty-claims fallback kick in) rather than raising --
   not explicitly tested (would require simulating a malformed real response), noted here for QA.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from rbaa.context import WorkContext
from rbaa.roles import Role

# Judgment call: `gemini-3.8-flash` confirmed as a real, current Gemini Flash model id against
# https://ai.google.dev/gemini-api/docs/models on 2026-10-04 (design.md flagged its own "Gemini
# 3.8 Flash" as unverified and asked for this confirmation at implementation time).
DEFAULT_GEMINI_TEXT_MODEL = "gemini-3.8-flash"

# Speaking-time formula, pinned by #10: estimated_seconds = word_count(script_text) / 150 * 60.
_WORDS_PER_MINUTE = 150

# Trim priority, pinned by #10: blockers survive first, then completed, then planned.
_TRIM_PRIORITY: dict[str, int] = {"blocker": 0, "completed": 1, "planned": 2}

_NO_ACTIVITY_TEXT = "No recorded activity since the last standup."

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

ClaimKind = Literal["completed", "planned", "blocker"]


class Claim(BaseModel):
    """One candidate (pre- or post-validation) standup claim.

    `kind` is pinned by #10's groomed body: the LLM assigns it in its draft, and the validator and
    trimmer trust it structurally (to decide which rule, and which trim tier, applies) but never
    trust the LLM's own evidence_ids without checking them against `context`.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    kind: ClaimKind


class StandupScript(BaseModel):
    """The drafted, validated, budget-trimmed standup update."""

    model_config = ConfigDict(frozen=True)

    script_text: str
    claims: list[Claim]


class LLMClient(Protocol):
    """The injectable interface `draft_standup` calls for the draft stage.

    Tests inject a fake implementing this; the real one is `GeminiClient` below. `generate` is
    expected to return raw text that is (or close to) a JSON array of claim-shaped objects -- see
    judgment call 9 in the module docstring for the exact contract and its tolerances.
    """

    async def generate(self, prompt: str, *, model: str) -> str: ...


class GeminiClient:
    """Default `LLMClient`, wrapping the `google-genai` SDK. Never constructed by a test that
    injects its own `client`, and never constructed for an empty context (see module docstring,
    judgment call 6) -- so the real SDK is only ever touched by the one `@pytest.mark.live` test or
    real production use, both of which need `GOOGLE_API_KEY` set.
    """

    def __init__(self) -> None:
        self._client = None

    async def generate(self, prompt: str, *, model: str) -> str:
        import asyncio

        if self._client is None:
            from google import genai

            self._client = genai.Client()

        def _call() -> str:
            interaction = self._client.interactions.create(model=model, input=prompt)
            return interaction.output_text

        return await asyncio.to_thread(_call)


def _resolve_model(model: str | None) -> str:
    """Precedence pinned by #10: explicit `model` param > `GEMINI_TEXT_MODEL` env var > default."""
    if model is not None:
        return model
    return os.environ.get("GEMINI_TEXT_MODEL", DEFAULT_GEMINI_TEXT_MODEL)


def _context_is_empty(context: WorkContext) -> bool:
    return not (
        context.commits
        or context.merged_prs
        or context.open_reviews
        or context.tickets
        or context.blockers
    )


def _build_prompt(role: Role, context: WorkContext) -> str:
    """Build the draft-stage prompt. Required by #10's acceptance criteria to include
    `role.communication_style` and `role.guidelines` verbatim."""
    context_json = json.dumps(context.model_dump(mode="json"), sort_keys=True)
    return (
        "You are drafting a spoken daily-standup update for a software engineering role.\n\n"
        f"Communication style: {role.communication_style}\n\n"
        f"Operational guidelines:\n{role.guidelines}\n\n"
        "Work context (JSON, evidence-id-bearing facts only -- cite only ids that literally "
        "appear in it):\n"
        f"{context_json}\n\n"
        "Return ONLY a JSON array of candidate claims, no prose, no markdown fences. Each claim "
        'is an object: {"text": str, "kind": "completed"|"planned"|"blocker", '
        '"evidence_ids": [str, ...]}. Cite only evidence_ids that literally appear in the work '
        "context above. If there is nothing to report in a category, omit claims for it; do not "
        "invent one."
    )


def _parse_claims(raw: str) -> list[Claim]:
    """Parse the model's raw response into candidate `Claim`s. Never raises: a response that does
    not parse into the expected shape yields no candidate claims (judgment call 9)."""
    text = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("claims", [])
    if not isinstance(data, list):
        return []
    claims: list[Claim] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            claims.append(Claim.model_validate(item))
        except Exception:  # noqa: BLE001 - a single malformed candidate is skipped, not fatal
            continue
    return claims


def validate_claims(claims: list[Claim], context: WorkContext) -> list[Claim]:
    """Deterministic, plain-Python grounding check (NFR-01) -- no model call. See the module
    docstring for rules (a), (b) and (c), and judgment calls 1-4.

    This is the only function allowed to decide what is grounded; `draft_standup` never trusts the
    LLM's own text for that decision.
    """
    # Rule (a) universe: judgment call 1 -- `open_reviews` is deliberately excluded.
    existing_ids = {c.evidence_id for c in context.commits}
    existing_ids |= {pr.evidence_id for pr in context.merged_prs}
    existing_ids |= {t.evidence_id for t in context.tickets}
    existing_ids |= {b.evidence_id for b in context.blockers}

    # Rule (b) universe: evidence that grounds "completed" work specifically.
    completed_ids = {c.evidence_id for c in context.commits}
    completed_ids |= {pr.evidence_id for pr in context.merged_prs}
    completed_ids |= {t.evidence_id for t in context.tickets if t.status_category == "Done"}

    # Rule (c) universe: evidence that grounds a "blocker" claim specifically.
    blocker_ids = {b.evidence_id for b in context.blockers}

    validated: list[Claim] = []
    for claim in claims:
        if not claim.evidence_ids:
            continue  # judgment call 3: no evidence at all fails rule (a)
        if not all(eid in existing_ids for eid in claim.evidence_ids):
            continue  # rule (a); judgment call 2: any bad id drops the whole claim
        if claim.kind == "completed" and not any(
            eid in completed_ids for eid in claim.evidence_ids
        ):
            continue  # rule (b)
        if claim.kind == "blocker" and not any(eid in blocker_ids for eid in claim.evidence_ids):
            continue  # rule (c)
        validated.append(claim)
    return validated


def _render_script(claims: list[Claim]) -> str:
    """Deterministically assemble `script_text` from already-validated, already-trimmed claims.
    Presentation order is Completed, Planned, Blockers (judgment call 8); falls back to the
    "no recorded activity" signal when there is nothing to say (judgment call 7)."""
    if not claims:
        return _NO_ACTIVITY_TEXT
    completed = [c.text for c in claims if c.kind == "completed"]
    planned = [c.text for c in claims if c.kind == "planned"]
    blockers = [c.text for c in claims if c.kind == "blocker"]
    sections: list[str] = []
    if completed:
        sections.append("Completed: " + " ".join(completed))
    if planned:
        sections.append("Planned: " + " ".join(planned))
    if blockers:
        sections.append("Blockers: " + " ".join(blockers))
    return " ".join(sections)


def _word_count(text: str) -> int:
    return len(text.split())


def _estimated_seconds(text: str) -> float:
    """Speaking-time formula, pinned by #10: word_count(script_text) / 150 * 60."""
    return _word_count(text) / _WORDS_PER_MINUTE * 60


def _trim_to_budget(claims: list[Claim], max_seconds: int) -> list[Claim]:
    """Drop whole claims, never partial text, until `_render_script` fits `max_seconds`.

    Cross-tier order pinned by #10: blockers survive first, then completed, then planned. Grouping
    claims into that order up front and then dropping from the tail implements exactly that:
    planned claims are dropped first (they are at the tail), then completed, and blockers only if
    nothing else is left. Within a tier, the last-ordered claim in that tier is dropped first
    (judgment call 5).
    """
    ordered = sorted(claims, key=lambda c: _TRIM_PRIORITY[c.kind])
    while ordered and _estimated_seconds(_render_script(ordered)) > max_seconds:
        lowest_priority = max(_TRIM_PRIORITY[c.kind] for c in ordered)
        for i in range(len(ordered) - 1, -1, -1):
            if _TRIM_PRIORITY[ordered[i].kind] == lowest_priority:
                del ordered[i]
                break
    return ordered


async def draft_standup(
    role: Role,
    context: WorkContext,
    *,
    model: str | None = None,
    client: LLMClient | None = None,
) -> StandupScript:
    """Draft, then validate, then trim a standup update for `role` from `context`.

    See the module docstring for the full two-stage architecture, validation rules, trimming
    algorithm and all disclosed judgment calls.
    """
    if _context_is_empty(context):
        # Judgment call 6: the model is never called when there is nothing to report.
        return StandupScript(script_text=_NO_ACTIVITY_TEXT, claims=[])

    resolved_model = _resolve_model(model)
    active_client: LLMClient = client if client is not None else GeminiClient()

    prompt = _build_prompt(role, context)
    raw = await active_client.generate(prompt, model=resolved_model)
    proposed = _parse_claims(raw)
    validated = validate_claims(proposed, context)
    trimmed = _trim_to_budget(validated, role.standup_max_seconds)
    script_text = _render_script(trimmed)
    return StandupScript(script_text=script_text, claims=trimmed)


__all__ = [
    "Claim",
    "ClaimKind",
    "StandupScript",
    "LLMClient",
    "GeminiClient",
    "DEFAULT_GEMINI_TEXT_MODEL",
    "validate_claims",
    "draft_standup",
]
