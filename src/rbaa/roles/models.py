"""The validated, read-only `Role` object."""

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ToolPermission = Literal["github:read", "jira:read"]
ALLOWED_TOOL_PERMISSIONS: tuple[str, ...] = get_args(ToolPermission)

# Strings are trimmed first, then must be non-empty.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RoleName = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]*$")]


class RoleFrontmatter(BaseModel):
    """The seven frontmatter fields of a role file."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: RoleName
    title: Text
    responsibilities: Annotated[list[Text], Field(min_length=1)]
    communication_style: Text
    standup_max_seconds: Annotated[int, Field(gt=0)]
    default_voice: Text
    tool_permissions: list[ToolPermission]

    @field_validator("tool_permissions")
    @classmethod
    def _no_duplicates(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            allowed = " and ".join(f"'{p}'" for p in ALLOWED_TOOL_PERMISSIONS)
            raise ValueError(f"duplicate values are not allowed (allowed values: {allowed})")
        return value


class Role(RoleFrontmatter):
    """A role: the frontmatter fields plus the Markdown body as operational guidelines."""

    guidelines: Text
