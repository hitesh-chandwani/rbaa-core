"""Load role files (Markdown with YAML frontmatter) into `Role` objects."""

from pathlib import Path
from typing import Any

import yaml
from frontmatter import YAMLHandler
from pydantic import ValidationError

from rbaa.roles.models import Role, RoleFrontmatter

DELIMITER = "---"


class RoleError(Exception):
    """A role file or roles directory is invalid. The message names the path and the problem."""


def _split(text: str, path: Path) -> tuple[str, str]:
    """Split file text into (frontmatter, body). The first line must be `---`."""
    lines = text.split("\n")
    if lines[0].rstrip() != DELIMITER:
        raise RoleError(f"{path}: frontmatter is missing (the file must start with a '---' line)")
    for index in range(1, len(lines)):
        if lines[index].rstrip() == DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise RoleError(f"{path}: frontmatter is unterminated (no closing '---' line)")


def _parse_yaml(frontmatter: str, path: Path) -> dict[str, Any]:
    try:
        # python-frontmatter's YAML handler loads with yaml.SafeLoader: no code is executed.
        data = YAMLHandler().load(frontmatter)
    except (yaml.YAMLError, ValueError) as exc:
        mark = getattr(exc, "problem_mark", None)
        where = ""
        if mark is not None:
            # +1 for 1-based, +1 for the opening '---' line, so this is the line in the file.
            where = f" at line {mark.line + 2}"
        problem = getattr(exc, "problem", None) or str(exc)
        raise RoleError(f"{path}: invalid YAML in frontmatter{where}: {problem}") from exc
    if not isinstance(data, dict):
        raise RoleError(f"{path}: frontmatter must be a key/value mapping")
    bad_keys = [key for key in data if not isinstance(key, str)]
    if bad_keys:
        raise RoleError(f"{path}: frontmatter field names must be strings, got {bad_keys[0]!r}")
    return data


def _field(location: tuple[int | str, ...]) -> str:
    field = str(location[0]) if location else "(frontmatter)"
    for part in location[1:]:
        field += f"[{part}]" if isinstance(part, int) else f".{part}"
    return field


def load_role(path: Path) -> Role:
    """Load one role file. Raises `RoleError` (naming `path`) if it is not a valid role."""
    try:
        text = path.read_bytes().decode("utf-8-sig")  # utf-8-sig drops a leading BOM
    except UnicodeDecodeError as exc:
        raise RoleError(f"{path}: file is not valid UTF-8 ({exc.reason})") from exc
    except OSError as exc:
        raise RoleError(f"{path}: cannot read file ({exc.strerror or exc})") from exc

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, body = _split(text, path)
    metadata = _parse_yaml(frontmatter, path)

    try:
        fields = RoleFrontmatter.model_validate(metadata)
    except ValidationError as exc:
        problems = "; ".join(f"{_field(e['loc'])}: {e['msg']}" for e in exc.errors())
        raise RoleError(f"{path}: {problems}") from exc

    body = body.strip()
    if not body:
        raise RoleError(f"{path}: the Markdown body (operational guidelines) is empty")
    return Role(**fields.model_dump(), guidelines=body)


def load_roles(directory: Path) -> dict[str, Role]:
    """Load every `*.md` file directly in `directory`, keyed by role `name`.

    Roles come back in ascending file-name order. Dotfiles, subdirectories and files that do not
    end in `.md` are ignored. All problems are reported together in one `RoleError`.
    """
    if not directory.exists():
        raise RoleError(f"{directory}: roles directory does not exist")
    if not directory.is_dir():
        raise RoleError(f"{directory}: not a directory")

    files = sorted(
        (p for p in directory.iterdir() if p.name.endswith(".md") and not p.name.startswith(".")),
        key=lambda p: p.name,
    )
    roles: dict[str, Role] = {}
    origin: dict[str, Path] = {}
    problems: list[str] = []
    for file in files:
        if not file.is_file():
            continue
        try:
            role = load_role(file)
        except RoleError as exc:
            problems.append(str(exc))
            continue
        if role.name in roles:
            problems.append(f"duplicate role name '{role.name}' in {origin[role.name]} and {file}")
            continue
        roles[role.name] = role
        origin[role.name] = file

    if problems:
        if len(problems) == 1:
            raise RoleError(problems[0])
        raise RoleError(f"{len(problems)} problems in {directory}:\n" + "\n".join(problems))
    return roles
