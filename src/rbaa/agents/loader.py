"""Load agent instance files (plain YAML) into `Agent` objects."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from rbaa.agents.models import Agent, AgentFrontmatter
from rbaa.roles import Role

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """An agent file or agents directory is invalid. The message names the path and the problem."""


def _field(location: tuple[int | str, ...]) -> str:
    field = str(location[0]) if location else "(file)"
    for part in location[1:]:
        field += f"[{part}]" if isinstance(part, int) else f".{part}"
    return field


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read `path` as UTF-8 YAML and return its top-level mapping."""
    try:
        text = path.read_bytes().decode("utf-8-sig")  # utf-8-sig drops a leading BOM
    except UnicodeDecodeError as exc:
        raise AgentError(f"{path}: file is not valid UTF-8 ({exc.reason})") from exc
    except OSError as exc:
        raise AgentError(f"{path}: cannot read file ({exc.strerror or exc})") from exc

    if not text.strip():
        raise AgentError(f"{path}: file is empty")

    try:
        # yaml.safe_load: no tag executes arbitrary code.
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise AgentError(f"{path}: invalid YAML{where}: {problem}") from exc

    if not isinstance(data, dict):
        raise AgentError(f"{path}: file must be a key/value mapping")
    bad_keys = [key for key in data if not isinstance(key, str)]
    if bad_keys:
        raise AgentError(f"{path}: field names must be strings, got {bad_keys[0]!r}")
    return data


def _load_frontmatter(path: Path, roles: dict[str, Role]) -> AgentFrontmatter:
    """Read and validate one agent file's fields, including that `role` is known.

    Does not touch the environment: token presence is checked separately, by `load_agent`.
    """
    data = _read_yaml_mapping(path)

    try:
        fields = AgentFrontmatter.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{_field(e['loc'])}: {e['msg']}" for e in exc.errors())
        raise AgentError(f"{path}: {problems}") from exc

    if fields.role not in roles:
        known = ", ".join(sorted(roles)) or "(none)"
        raise AgentError(
            f"{path}: agent '{fields.agent_id}' has unknown role '{fields.role}' "
            f"(known roles: {known})"
        )
    return fields


def _with_tokens(path: Path, fields: AgentFrontmatter) -> Agent:
    """Resolve `fields`' token environment variables and build the final `Agent`."""
    problems: list[str] = []
    for var_field, var_name in (
        ("github_token_env", fields.github_token_env),
        ("jira_token_env", fields.jira_token_env),
    ):
        if not os.environ.get(var_name):
            problems.append(
                f"environment variable '{var_name}' ({var_field}) for agent "
                f"'{fields.agent_id}' is unset or empty"
            )
    if problems:
        raise AgentError(f"{path}: " + "; ".join(problems))

    logger.debug("loaded agent '%s' (role '%s') from %s", fields.agent_id, fields.role, path)
    return Agent(
        **fields.model_dump(),
        github_token=SecretStr(os.environ[fields.github_token_env]),
        jira_token=SecretStr(os.environ[fields.jira_token_env]),
    )


def load_agent(path: Path, roles: dict[str, Role]) -> Agent:
    """Load one agent file. Raises `AgentError` (naming `path`) if it is not a valid agent.

    `roles` is the mapping `load_roles` returns: role `name` to `Role`.
    """
    fields = _load_frontmatter(path, roles)
    return _with_tokens(path, fields)


def _duplicate_problems(
    parsed: list[tuple[Path, AgentFrontmatter]], *, key: Any, label: str
) -> list[str]:
    """Report each repeated `key(fields)` against the first file that used it."""
    seen: dict[Any, tuple[Path, AgentFrontmatter]] = {}
    problems: list[str] = []
    for file, fields in parsed:
        value = key(fields)
        if value in seen:
            other_file, other_fields = seen[value]
            problems.append(
                f"duplicate {label} {value!r}: agent '{other_fields.agent_id}' ({other_file}) "
                f"and agent '{fields.agent_id}' ({file})"
            )
        else:
            seen[value] = (file, fields)
    return problems


def _duplicate_token_env_problems(parsed: list[tuple[Path, AgentFrontmatter]]) -> list[str]:
    """Report a token environment variable name reused by more than one agent (NFR-04)."""
    seen: dict[str, tuple[Path, AgentFrontmatter, str]] = {}
    problems: list[str] = []
    for file, fields in parsed:
        for env_field in ("github_token_env", "jira_token_env"):
            value = getattr(fields, env_field)
            if value in seen:
                other_file, other_fields, other_field = seen[value]
                problems.append(
                    f"duplicate token environment variable {value!r}: agent "
                    f"'{other_fields.agent_id}' ({other_field} in {other_file}) and agent "
                    f"'{fields.agent_id}' ({env_field} in {file})"
                )
            else:
                seen[value] = (file, fields, env_field)
    return problems


def _cross_file_problems(parsed: list[tuple[Path, AgentFrontmatter]]) -> list[str]:
    problems: list[str] = []
    problems += _duplicate_problems(parsed, key=lambda f: f.agent_id, label="agent_id")
    problems += _duplicate_problems(
        parsed, key=lambda f: f.display_name.lower(), label="display_name (case-insensitive)"
    )
    problems += _duplicate_problems(
        parsed,
        key=lambda f: f.github_username.lower(),
        label="github_username (case-insensitive)",
    )
    problems += _duplicate_problems(
        parsed, key=lambda f: f.jira_account_id, label="jira_account_id"
    )
    problems += _duplicate_token_env_problems(parsed)
    return problems


def load_agents(directory: Path, roles: dict[str, Role]) -> dict[str, Agent]:
    """Load every `*.yaml` file directly in `directory`, keyed by `agent_id`.

    Dotfiles, subdirectories and files that do not end in `.yaml` are ignored. Every problem in
    every file, plus cross-file rules (duplicate `agent_id`, `display_name`, `github_username`,
    `jira_account_id` or token environment variable), is reported together in one `AgentError`.
    """
    if not directory.exists():
        raise AgentError(f"{directory}: agents directory does not exist")
    if not directory.is_dir():
        raise AgentError(f"{directory}: not a directory")

    files = sorted(
        (p for p in directory.iterdir() if p.name.endswith(".yaml") and not p.name.startswith(".")),
        key=lambda p: p.name,
    )

    problems: list[str] = []
    parsed: list[tuple[Path, AgentFrontmatter]] = []
    for file in files:
        if not file.is_file():
            continue
        try:
            parsed.append((file, _load_frontmatter(file, roles)))
        except AgentError as exc:
            problems.append(str(exc))

    problems.extend(_cross_file_problems(parsed))

    agents: dict[str, Agent] = {}
    for file, fields in parsed:
        try:
            agents[fields.agent_id] = _with_tokens(file, fields)
        except AgentError as exc:
            problems.append(str(exc))

    if problems:
        if len(problems) == 1:
            raise AgentError(problems[0])
        raise AgentError(f"{len(problems)} problems in {directory}:\n" + "\n".join(problems))
    return agents
