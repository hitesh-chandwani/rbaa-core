"""Roles as configuration: a role is a Markdown file with YAML frontmatter.

Adding a role means adding a `.md` file to the roles directory, not writing code.

File format
-----------

A role file starts with a line `---`, then YAML, then a line `---`, then the Markdown body::

    ---
    name: product_manager
    title: Product Manager
    responsibilities:
      - Own the product backlog
      - Report priorities and blockers
    communication_style: Concise and outcome-focused
    standup_max_seconds: 75
    default_voice: Kore
    tool_permissions:
      - github:read
      - jira:read
    ---
    Operational guidelines in Markdown. Only claim work you can back with a ticket or commit.

All seven frontmatter fields are required (the key must be present). Unknown fields are rejected.

- `name`: lowercase identifier matching `^[a-z][a-z0-9_]*$` (for example `product_manager`). It is
  the key an agent's `role` refers to and the key `load_roles` returns. It does not have to equal
  the file name, and two files must not use the same `name`. Quote it if YAML would read it as a
  boolean or number.
- `title`: non-empty string, the human-readable job title.
- `responsibilities`: list of non-empty strings, at least one.
- `communication_style`: non-empty string.
- `standup_max_seconds`: integer greater than 0 (`"90"`, `90.5` and `true` are not integers).
- `default_voice`: non-empty string. It is not checked against any voice list here.
- `tool_permissions`: list of unique values, each `github:read` or `jira:read`. An empty list is
  valid.

Strings are trimmed of leading and trailing whitespace; one that is empty after trimming is an
error. The body is everything after the closing `---`, trimmed; it becomes the role's
`guidelines`. An empty or whitespace-only body is an error. A line that is just `---` inside the
body is ordinary Markdown (a horizontal rule). YAML comments (`# ...`) are allowed in the
frontmatter. Files may be UTF-8 (with or without a byte order mark) and use LF or CRLF line
endings. YAML is loaded safely: tags that would construct Python objects are errors.

Loading
-------

- `load_role(path)` returns a `Role` (frozen: assigning to a field raises).
- `load_roles(directory)` returns `{name: Role}` in ascending file-name order. It reads only files
  ending in `.md` directly in the directory (dotfiles, subdirectories and other extensions are
  ignored), so every other `.md` file there, `README.md` included, must be a valid role.
- Everything that goes wrong raises `RoleError`, whose message names the file and the field.
  `load_roles` collects all invalid files and duplicate names into a single `RoleError`.
"""

from rbaa.roles.loader import RoleError, load_role, load_roles
from rbaa.roles.models import Role

__all__ = ["Role", "RoleError", "load_role", "load_roles"]
