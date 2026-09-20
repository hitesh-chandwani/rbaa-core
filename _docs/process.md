# How work is organized

Work is tracked as GitHub issues. Each issue moves through three roles, defined in `team/`.

| Step | Role | Definition | Output |
| :--- | :--- | :--- | :--- |
| 1. Groom | Product Manager | [`team/pm.md`](../team/pm.md) | Issue rewritten with the four sections of [`task-template.md`](task-template.md); follow-up issues filed for anything moved out of scope |
| 2. Implement | Software Engineer | [`team/software-engineer.md`](../team/software-engineer.md) | Committed code and tests; a comment on the issue saying what was done; issue left open |
| 3. Verify | QA Engineer | [`team/qa-engineer.md`](../team/qa-engineer.md) | A `## QA: PASS` or `## QA: FAIL` comment with a verdict per acceptance criterion |

## Rules that apply to every step

- The acceptance criteria in the issue are the contract. The engineer implements them and does not change them. If one is wrong or impossible, the engineer comments on the issue.
- The PM writes no code. QA fixes nothing; it reports.
- Anything that does not belong in a task goes into a new issue and is linked under "Out of scope".
- One issue is implemented at a time, and only after it has been groomed.
- Dependencies are listed under "Constraints" as `Depends on #N`; do not start an issue whose dependencies are unfinished.

## Where things live

- Product requirements and roadmap: [`plan.md`](../plan.md)
- Technology choices and architecture: [`design.md`](../design.md)
- Roles for developing this repo: `team/`. Roles for the meeting agents the product runs: `roles/` (created in issue #4).

## Labels

- `mvp` / `post-mvp`: in or out of the first release
- `phase-0` to `phase-5`: roadmap phase from `plan.md`
- `spike`: produces a report, not product code
- `conditional`: only starts if another issue's outcome requires it
- `nfr`: non-functional requirement work
