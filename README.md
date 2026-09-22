# rbaa-core

Role-Based Autonomous Meeting Agents. See `_docs/design.md` for the architecture.

This scaffold runs three services with Docker Compose: `app` (FastAPI, Python 3.12), `postgres`
(with pgvector) and `redis`.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Compose v2 (`docker compose`)
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you)

## Quick start

```sh
cp .env.example .env            # local settings; .env is git-ignored
docker compose up --build --wait   # builds the app image, starts everything, waits until healthy
docker compose ps               # app, postgres and redis should all be "healthy"

curl -i localhost:8000/health   # 200 {"status":"ok"}
curl -i localhost:8000/ready    # 200 {"status":"ready"}
```

The values in `.env.example` are non-secret defaults for **local development only**. Do not reuse
them anywhere else. `GOOGLE_API_KEY` is empty on purpose: the app reads it but does not use or
validate it yet. Put a real key only in `.env`, never in `.env.example`.

Postgres and Redis are not published on host ports, so a Postgres already running on 5432 or a
Redis on 6379 cannot block startup. The app is on host port 8000; change it with `APP_PORT` in
`.env` (for example `APP_PORT=8123`, then `curl localhost:8123/health`).

### Endpoints

| Endpoint | Meaning |
| --- | --- |
| `GET /health` | The process is up. Never touches Postgres or Redis. Always `200 {"status":"ok"}`. |
| `GET /ready` | Postgres (`SELECT 1`) and Redis (`PING`) both answered: `200 {"status":"ready"}`. Otherwise `503 {"status":"unavailable","unavailable":["postgres","redis"]}` listing the failed ones. It answers within a few seconds even when a dependency is down. |

## Checking the services

```sh
# pgvector is installed and usable
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;" -c "SELECT '"'"'[1,2,3]'"'"'::vector;"'

# Redis answers
docker compose exec redis redis-cli ping     # PONG

# Python version inside the app container
docker compose exec app python --version     # Python 3.12.x
```

## Tests and lint

Tests need no network and no running containers.

```sh
uv sync                        # install locked dependencies (creates .venv)
uv run pytest                  # run the test suite
uv run ruff check .            # lint
uv run ruff format --check .   # format check (use `uv run ruff format .` to fix)
```

## Stopping and wiping data

```sh
docker compose stop            # stop, keep data
docker compose down            # remove containers, keep the Postgres data volume
docker compose down -v         # remove containers AND the Postgres data volume (wipes data)
```

## Layout

- `src/rbaa/` - the application (`rbaa.main:app` is the FastAPI object)
- `roles/` - the role files that define the meeting agents (Product Manager, Senior Software
  Engineer, QA Engineer). One `.md` file per role; adding a role means adding a file, not code.
  The file format is documented in the docstring of `src/rbaa/roles/__init__.py`. Every `.md` file
  in this directory is loaded as a role, so do not put a `README.md` or notes there.
- `agents/` - agent instance files: each binds one role (from `roles/`) to one set of external
  identities and credential references (a GitHub/Jira username and the *names* of the environment
  variables holding their tokens, never the tokens themselves). One `.yaml` file per agent
  instance; `agents/example.yaml` documents every field. The file format is documented in the
  docstring of `src/rbaa/agents/__init__.py`.
- `_docs/team/` - the role files for the people and AI agents who develop this repo (PM, engineer,
  QA). These are not the meeting agents in `roles/`.
- `tests/` - tests
- `Dockerfile`, `docker-compose.yml`, `.env.example` - local stack
- `uv.lock` - locked dependencies; the image installs from it with `uv sync --frozen`
