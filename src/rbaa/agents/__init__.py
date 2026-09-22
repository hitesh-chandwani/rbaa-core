"""Agent instances: one role bound to one set of external identities and credential references.

An agent instance is a plain YAML file in `agents/` (see `agents/example.yaml`). It binds one role
(the `name` of a role loaded by `rbaa.roles.load_roles`, see `src/rbaa/roles/__init__.py`) to one
set of external identities. Tokens are referenced by environment variable name only: they never
appear in an agent file, and are held only as `pydantic.SecretStr` once loaded.

File format
-----------

Each `agents/*.yaml` file defines one agent with exactly these fields, all required::

    agent_id: devops_bot
    role: senior_software_engineer
    display_name: DevOps Bot
    github_username: devops-bot
    jira_account_id: 5f8c1e2a9b0c3d4e5f6a7b8c
    github_token_env: DEVOPS_BOT_GITHUB_TOKEN
    jira_token_env: DEVOPS_BOT_JIRA_TOKEN
    allowed_repos:
      - my-org/my-repo
    allowed_jira_projects:
      - OPS

- `agent_id`: lowercase identifier matching `^[a-z][a-z0-9_-]{0,63}$`. Unique across all agents.
- `role`: the `name` of a role known to the loader. Matched case-sensitively.
- `display_name`: non-empty after trimming. Unique across all agents, compared
  case-insensitively after trimming (so `Sam` and `sam ` collide).
- `github_username`: non-empty. Unique across all agents, compared case-insensitively.
- `jira_account_id`: non-empty. Unique across all agents (case-sensitive).
- `github_token_env`, `jira_token_env`: the name of an environment variable holding the token,
  matching `^[A-Za-z_][A-Za-z0-9_]*$`. Never the token itself. No variable name may be reused by
  more than one agent, in either field (NFR-04: credentials are isolated per agent).
- `allowed_repos`: list of `owner/name` GitHub repositories (letters, digits, `.`, `_`, `-` only).
  `allowed_jira_projects`: list of Jira project keys matching `^[A-Z][A-Z0-9_]+$`. Both may be
  empty, meaning the agent may access none; duplicate entries within a list are an error.

Unknown fields (including a `github_token` or `jira_token` key) are rejected. Enforcing
`allowed_repos`/`allowed_jira_projects` and fetching GitHub/Jira data are out of scope here.

Loading
-------

- `load_agent(path, roles)` returns an `Agent` (frozen). `roles` is what `load_roles` returns.
- `load_agents(directory, roles)` returns `{agent_id: Agent}` for every `*.yaml` file directly in
  `directory` (dotfiles, subdirectories and other extensions are ignored).
- Everything that goes wrong raises `AgentError`, whose message names the file and the field.
  `load_agents` collects every invalid file and every cross-file problem into one `AgentError`.
- A token environment variable that is unset or empty at load time raises `AgentError` naming the
  variable and the agent, never a value. Loaded tokens are `pydantic.SecretStr`: reading one back
  requires an explicit `.get_secret_value()` call, and the value never appears in `repr()`,
  `str()`, `model_dump()`, `model_dump_json()` or log output.
"""

from rbaa.agents.loader import AgentError, load_agent, load_agents
from rbaa.agents.models import Agent

__all__ = ["Agent", "AgentError", "load_agent", "load_agents"]
