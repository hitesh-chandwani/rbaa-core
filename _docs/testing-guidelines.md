# Testing guidelines

Read this before writing tests. It applies to the engineer who writes them (`_docs/team/software-engineer.md`) and the QA engineer who runs them (`_docs/team/qa-engineer.md`).

## Commands

```bash
uv run pytest                 # default suite: fast, offline, no API keys needed
uv run pytest -m live         # opt-in: real Gemini / Attendee / GitHub / Jira calls
uv run ruff check . && uv run ruff format --check .
```

The default suite must pass on a fresh clone with no `.env`, no network and no running containers (the README promises this). If a test needs a network call or a key, it is a `live` test. If it needs Postgres or Redis, it is an `integration` test.

## Three kinds of test

| Kind | Marker | Runs by default | Talks to |
| :--- | :--- | :--- | :--- |
| Unit / component | none | Yes | Nothing external. Fakes and recorded responses only |
| Integration | `integration` | Runs when the Compose Postgres/Redis are reachable, otherwise skipped with the reason | Local Postgres (pgvector) and Redis from `docker-compose.yml` |
| Live | `live` | **No** | Real Gemini, Attendee, GitHub, Jira, Google Meet |

Live tests are skipped unless `-m live` is passed and the required environment variables are set. A skipped test always states why (which variable is missing, or which service is unreachable).

## Tooling to add when first needed

The scaffold currently has only `pytest` and `ruff`. Add the following in the issue that first needs it, and record it in `pyproject.toml`:

- An HTTP mocking approach (first needed by #7). The project uses `httpx2`, and `respx` targets the original `httpx`, so check that `respx` works with `httpx2` before adopting it. Otherwise use `httpx2`'s own mock transport. Record the choice here.
- `pytest-asyncio` for async tests (first needed by #11 or #12)
- the `live` and `integration` markers, registered under `[tool.pytest.ini_options]`, plus the skip logic in `tests/conftest.py`

## What to fake, and how

- **GitHub and Jira (#7, #8):** mock HTTP at the client transport (see "Tooling to add"). Store recorded responses as JSON in `tests/fixtures/http/<service>/`. Include the awkward ones: multi-page results, 403/429 with reset headers, 404 on one repo, and empty results. Tests never call the real API.
- **Gemini text model (#10, #17):** the client sits behind an interface; tests inject a fake that returns recorded responses. Any assertion about model wording belongs in a `live` test, not the default suite.
- **Gemini Live (#11):** tests use a fake Live server that speaks the same WebSocket messages, including `interrupted`, a dropped connection, a resumption handle, and tool calls.
- **Attendee (#12, #13):** tests use a fake Attendee WebSocket that sends and receives base64 16-bit mono PCM. Bridge tests must cover a slow consumer and a mid-stream disconnect.
- **Time:** never call the system clock in logic under test. Inject a clock. This is required for the "since" default (#9), Monday-to-Friday lookback, scheduling, DST (#20) and speaking-time limits.
- **Database (#18, #19):** test against real Postgres with pgvector, not SQLite. Each test uses a transaction or fresh schema so tests do not depend on order.

## Fixtures

| Path | Contents |
| :--- | :--- |
| `tests/fixtures/roles/` | Valid and invalid role files (#3) |
| `tests/fixtures/http/` | Recorded GitHub and Jira responses |
| `tests/fixtures/audio/` | Short PCM/WAV clips: speech, tones, noise, silence (#12, #14, #21) |
| `tests/fixtures/utterances/` | Labelled addressed/not-addressed utterances for name detection (#14) |

Keep audio clips short (a few seconds) and commit them as-is. Do not record real meetings or real people's voices; use synthetic or self-recorded clips.

## Rules specific to this project

1. **Grounding is tested, not assumed (NFR-01).** For anything that produces text the agent will speak, include a test that injects an unsupported claim and asserts it does not reach the output. Cover the case where the only evidence is an open PR or an In Progress ticket.
2. **Missing-context behaviour (FR-06).** Tests for Q&A include questions whose answer is not in the context or tools, and assert that nothing is asserted as fact.
3. **Isolation (NFR-04).** Tests seed a token with a known string such as `SECRET123` and assert it never appears in logs, exceptions, `repr()`, payloads or stored rows. For multi-agent code, assert one agent's credentials never appear on another's requests.
4. **No credentials in tests.** Use fake tokens. A live test reads real ones from the environment and never prints them.
5. **Determinism.** The same inputs and clock must give byte-identical output where the issue says so (#9). Test this by running twice and comparing.
6. **Threshold tests are live.** Criteria phrased as "at least 9 of 10" (#11, #14) or as latency limits (#21) measure a real model or network. Put them behind `live` and report the numbers. The default suite checks the logic (counting, thresholds, PASS/FAIL output) with fixed inputs.
7. **Audio assertions use numbers, not ears.** Check frequency, duration, sample rate, sample count or transcript text. Anything only a human can judge, such as "a person in the meeting hears it", is a manual check and is written up in the issue comment, not hidden in a test.
8. **Async code is tested as async.** Use `pytest-asyncio`. Give every test that touches a socket or queue an explicit timeout so a hang fails instead of stalling the run.

## Structure and naming

- Test files are flat in `tests/` and named after the module they cover: `src/rbaa/roles/loader.py` is tested in `tests/test_loader.py`. Introduce a subdirectory only when a package grows enough files to need it.
- Name tests after the behaviour, for example `test_duplicate_display_name_is_rejected`, not `test_agents_2`.
- One behaviour per test. Prefer a small table of cases (`pytest.mark.parametrize`) over copy-pasted tests.
- Test the public behaviour named in the acceptance criteria. Do not test private helpers just to raise coverage.

## Mapping tests to acceptance criteria

- Every acceptance criterion in the issue has either an automated test or an explicit manual check.
- The engineer's comment on the issue lists each criterion with the test that covers it, or the manual step taken.
- The QA engineer runs the default suite, and `-m live` only when the criterion needs it and the keys are available. The QA comment names the exact commands run and their result, as shown in `_docs/team/qa-engineer.md`. A criterion that could only be checked by a live test that was not run is reported as **not verified**, and the verdict is FAIL.

## Definition of a good test run

- `uv run pytest` passes with zero failures, and skipped tests are only `live` ones, plus `integration` ones when the containers are not running. Each skip gives its reason.
- No test writes outside a temporary directory, or into `out/` or a real database.
- No test depends on the order in which tests run or on data left by another test.
