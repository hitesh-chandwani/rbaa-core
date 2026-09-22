"""The validated, read-only `Agent` object."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, field_validator

# Strings are trimmed first, then must be non-empty.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
AgentId = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
]
EnvVarName = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
]
RepoName = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
]
JiraProjectKey = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_]+$")
]


def _reject_duplicates(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        raise ValueError("duplicate entries are not allowed")
    return value


class AgentFrontmatter(BaseModel):
    """The nine fields of an agent file.

    No token values here: only the names of the environment variables that hold them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    agent_id: AgentId
    role: Text
    display_name: Text
    github_username: Text
    jira_account_id: Text
    github_token_env: EnvVarName
    jira_token_env: EnvVarName
    allowed_repos: list[RepoName]
    allowed_jira_projects: list[JiraProjectKey]

    @field_validator("allowed_repos", "allowed_jira_projects")
    @classmethod
    def _no_duplicates(cls, value: list[str]) -> list[str]:
        return _reject_duplicates(value)


class Agent(AgentFrontmatter):
    """An agent instance: the frontmatter fields plus tokens read from the environment.

    Tokens are held only as `SecretStr`; reading the value back requires an explicit call to
    `.get_secret_value()`. They never appear in `repr()`, `str()`, `model_dump()` or
    `model_dump_json()`.
    """

    github_token: SecretStr
    jira_token: SecretStr
