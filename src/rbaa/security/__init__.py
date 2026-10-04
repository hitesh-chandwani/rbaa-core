"""Per-agent credential and permission isolation guard (NFR-04, design.md §3 "Multi-agent").

`require_permission(agent, action, resource, roles)` is the single gate every GitHub/Jira
fetcher (#7, #8) must call before building a real client. It never makes a network call itself:
it only decides, from one agent's own config, whether that agent may perform `action` on
`resource`, and raises before any client is built if it may not.

- `action` is one of `"github:read"`, `"github:write"`, `"jira:read"`, `"jira:write"`.
- `resource` is a GitHub repo `"owner/name"` (for `github:*` actions) or a Jira project key (for
  `jira:*` actions).
- `roles` is the mapping `rbaa.roles.load_roles` returns (role `name` -> `Role`) - the same
  mapping already passed to `rbaa.agents.load_agent`/`load_agents` to resolve `agent.role`. It is
  needed here because `Agent` stores only its role's *name*, not the role's `tool_permissions`.

Two independent checks, both of which must pass:

1. **Write actions are always denied.** Per #3/#4, a role's `tool_permissions` can only ever
   contain `"github:read"` or `"jira:read"` today - no write value exists yet. Until one does,
   `require_permission` raises `PermissionDenied` for `"github:write"` and `"jira:write"` for
   every agent, without even consulting `roles` or `agent.allowed_repos`/`allowed_jira_projects`.
   There is no exception to this rule.
2. **Read actions need both a role grant and a resource grant.** For `"github:read"` /
   `"jira:read"`, the agent's role (`roles[agent.role]`) must list `action` in its
   `tool_permissions`, AND `resource` must appear in `agent.allowed_repos` (for `github:read`) or
   `agent.allowed_jira_projects` (for `jira:read`). Either check failing raises
   `PermissionDenied`.

Every denial is logged with the agent id and the resource - never a token or any other secret -
before `PermissionDenied` is raised. The exception itself (`str()` and `repr()`) also never
carries a token: it is built only from the agent id, action, resource and a short fixed reason.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import NoReturn

from rbaa.agents import Agent
from rbaa.roles import Role

logger = logging.getLogger(__name__)

# Read action -> the `Agent` field listing the resources it may touch.
_READ_ACTIONS: dict[str, str] = {
    "github:read": "allowed_repos",
    "jira:read": "allowed_jira_projects",
}
_WRITE_ACTIONS: frozenset[str] = frozenset({"github:write", "jira:write"})
_KNOWN_ACTIONS: frozenset[str] = frozenset(_READ_ACTIONS) | _WRITE_ACTIONS


class PermissionDenied(Exception):
    """`agent` may not perform `action` on `resource`.

    Carries only the agent id, action, resource and a short, fixed reason - never a token or any
    other secret - in its message, `str()`, and `repr()`.
    """

    def __init__(self, agent_id: str, action: str, resource: str, reason: str) -> None:
        self.agent_id = agent_id
        self.action = action
        self.resource = resource
        self.reason = reason
        super().__init__(f"agent {agent_id!r} may not {action!r} on {resource!r}: {reason}")


def _deny(agent: Agent, action: str, resource: str, reason: str) -> NoReturn:
    """Log the denial (agent id and resource, never a token) and raise `PermissionDenied`."""
    logger.warning(
        "permission denied: agent=%s action=%s resource=%s reason=%s",
        agent.agent_id,
        action,
        resource,
        reason,
    )
    raise PermissionDenied(agent.agent_id, action, resource, reason)


def require_permission(
    agent: Agent,
    action: str,
    resource: str,
    roles: Mapping[str, Role],
) -> None:
    """Raise `PermissionDenied` unless `agent` may perform `action` on `resource`.

    Makes no network call. Call this before building any GitHub/Jira client (#7, #8). `roles` is
    the mapping `rbaa.roles.load_roles` returns, used to resolve `agent.role`'s `tool_permissions`.
    """
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unknown action {action!r}; must be one of {sorted(_KNOWN_ACTIONS)}")

    if action in _WRITE_ACTIONS:
        # No role's tool_permissions can contain a write value yet (#3/#4): always deny, for
        # every agent, without looking at roles or allowed_repos/allowed_jira_projects.
        _deny(agent, action, resource, "no write permission exists yet")

    role = roles.get(agent.role)
    if role is None:
        _deny(agent, action, resource, f"role {agent.role!r} is not known")
    if action not in role.tool_permissions:
        _deny(agent, action, resource, "agent's role does not grant this action")

    allowed = getattr(agent, _READ_ACTIONS[action])
    if resource not in allowed:
        _deny(agent, action, resource, "resource is not in the agent's allowed list")


__all__ = ["PermissionDenied", "require_permission"]
