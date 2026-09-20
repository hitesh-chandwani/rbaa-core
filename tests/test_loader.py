import shutil
from pathlib import Path

import pytest

from rbaa.roles import Role, RoleError, load_role, load_roles

FIXTURES = Path(__file__).parent / "fixtures" / "roles"
VALID = FIXTURES / "valid.md"

# The frontmatter of a valid role, as raw YAML per field, so tests can change one field at a time.
FIELDS = {
    "name": "product_manager",
    "title": "Product Manager",
    "responsibilities": "\n  - Own the backlog",
    "communication_style": "Concise",
    "standup_max_seconds": "75",
    "default_voice": "Kore",
    "tool_permissions": "\n  - github:read",
}
BODY = "Guidelines."


def role_text(body: str = BODY, extra: str = "", **overrides: str | None) -> str:
    """A role file's text. `overrides` replaces a field's raw YAML; None drops the field."""
    fields = {**FIELDS, **overrides}
    lines = [f"{key}: {value}" for key, value in fields.items() if value is not None]
    return "---\n" + "\n".join(lines) + "\n" + extra + "---\n" + body + "\n"


def write(tmp_path: Path, text: str | bytes, name: str = "role.md") -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def error_of(path: Path) -> str:
    with pytest.raises(RoleError) as info:
        load_role(path)
    return str(info.value)


# --- loading a valid file ---


def test_valid_fixture_loads_every_field():
    role = load_role(VALID)

    assert isinstance(role, Role)
    assert role.name == "product_manager"
    assert role.title == "Product Manager"
    assert role.responsibilities == [
        "Own the product backlog",
        "Résumé the sprint goals for the team",
    ]
    assert role.communication_style == "Concise, outcome-focused, plain language"
    assert role.standup_max_seconds == 75
    assert role.default_voice == "Kore"
    assert role.tool_permissions == ["github:read", "jira:read"]


def test_valid_fixture_body_is_kept_exactly_including_horizontal_rule():
    role = load_role(VALID)

    assert role.guidelines == (
        "# Operating guidelines\n\n"
        "Report only work you can back with a ticket or a commit.\n\n"
        "Keep answers short. If you do not know, say so.\n\n"
        "---\n\n"
        "Second section after a horizontal rule."
    )


def test_crlf_line_endings_load_to_the_same_role(tmp_path):
    crlf = VALID.read_bytes().replace(b"\n", b"\r\n")

    assert load_role(write(tmp_path, crlf)) == load_role(VALID)


def test_utf8_byte_order_mark_loads_to_the_same_role(tmp_path):
    with_bom = b"\xef\xbb\xbf" + VALID.read_bytes()

    assert load_role(write(tmp_path, with_bom)) == load_role(VALID)


def test_loaded_role_is_read_only():
    role = load_role(VALID)

    with pytest.raises(ValueError):
        role.title = "Someone Else"


def test_empty_tool_permissions_is_valid(tmp_path):
    role = load_role(write(tmp_path, role_text(tool_permissions="[]")))

    assert role.tool_permissions == []


def test_strings_are_trimmed(tmp_path):
    text = role_text(title='"  Product Manager  "', responsibilities='\n  - "  Plan  "')

    role = load_role(write(tmp_path, text))

    assert role.title == "Product Manager"
    assert role.responsibilities == ["Plan"]


def test_body_is_trimmed(tmp_path):
    role = load_role(write(tmp_path, role_text(body="\n\n  Guidelines.  \n\n")))

    assert role.guidelines == "Guidelines."


def test_name_may_differ_from_file_name(tmp_path):
    role = load_role(write(tmp_path, role_text(), name="something_else.md"))

    assert role.name == "product_manager"


def test_string_field_may_hold_a_colon_when_quoted(tmp_path):
    role = load_role(write(tmp_path, role_text(communication_style='"Short: and clear"')))

    assert role.communication_style == "Short: and clear"


# --- errors on one file ---


@pytest.mark.parametrize("field", list(FIELDS))
def test_missing_required_field_names_file_and_field(tmp_path, field):
    path = write(tmp_path, role_text(**{field: None}))

    message = error_of(path)

    assert str(path) in message
    assert field in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "123"),
        ("title", "[a, b]"),
        ("responsibilities", '"just text"'),
        ("responsibilities", "[1, 2]"),
        ("communication_style", "true"),
        ("default_voice", "5"),
        ("tool_permissions", "github:read"),
        ("tool_permissions", "[1]"),
    ],
)
def test_field_of_wrong_type_names_file_and_field(tmp_path, field, value):
    path = write(tmp_path, role_text(**{field: value}))

    message = error_of(path)

    assert str(path) in message
    assert field in message


@pytest.mark.parametrize("field", ["title", "communication_style", "default_voice"])
@pytest.mark.parametrize("value", ['""', '"   "'])
def test_empty_string_field_names_file_and_field(tmp_path, field, value):
    path = write(tmp_path, role_text(**{field: value}))

    message = error_of(path)

    assert str(path) in message
    assert field in message


@pytest.mark.parametrize("value", ["[]", '["  "]', '[""]'])
def test_responsibilities_needs_at_least_one_non_empty_item(tmp_path, value):
    path = write(tmp_path, role_text(responsibilities=value))

    message = error_of(path)

    assert str(path) in message
    assert "responsibilities" in message


@pytest.mark.parametrize("value", ['"90"', "90.5", "90.0", "true", "null", "0", "-5"])
def test_invalid_standup_max_seconds_names_file_and_field(tmp_path, value):
    path = write(tmp_path, role_text(standup_max_seconds=value))

    message = error_of(path)

    assert str(path) in message
    assert "standup_max_seconds" in message


def test_tool_permission_not_allowed_lists_allowed_values(tmp_path):
    path = write(tmp_path, role_text(tool_permissions="[github:write]"))

    message = error_of(path)

    assert str(path) in message
    assert "github:read" in message
    assert "jira:read" in message


def test_duplicate_tool_permission_lists_allowed_values(tmp_path):
    path = write(tmp_path, role_text(tool_permissions="[github:read, github:read]"))

    message = error_of(path)

    assert str(path) in message
    assert "duplicate" in message
    assert "github:read" in message
    assert "jira:read" in message


def test_unknown_field_is_rejected_and_named(tmp_path):
    path = write(tmp_path, role_text(responsibilites="[typo]"))

    message = error_of(path)

    assert str(path) in message
    assert "responsibilites" in message


def test_frontmatter_key_named_like_the_body_field_is_rejected(tmp_path):
    path = write(tmp_path, role_text(guidelines="sneaky"))

    message = error_of(path)

    assert str(path) in message
    assert "guidelines" in message


@pytest.mark.parametrize(
    "value",
    ['"Product Manager"', "1st_role", '"_leading"', '"UPPER"', '"has-dash"', "no", "true", '""'],
)
def test_invalid_name_names_file_and_field(tmp_path, value):
    path = write(tmp_path, role_text(name=value))

    message = error_of(path)

    assert str(path) in message
    assert "name" in message


def test_malformed_yaml_names_file_and_line_number(tmp_path):
    text = "---\nname: product_manager\ntitle: [unclosed\nstandup_max_seconds: 75\n---\nBody\n"
    path = write(tmp_path, text)

    message = error_of(path)

    assert str(path) in message
    assert "line 4" in message  # the parser reports the problem where the flow list breaks


def test_malformed_yaml_line_number_is_the_line_in_the_file(tmp_path):
    text = "---\nname: product_manager\n\tbad: tab\n---\nBody\n"
    path = write(tmp_path, text)

    message = error_of(path)

    assert str(path) in message
    assert "line 3" in message


def test_file_without_frontmatter_says_frontmatter_is_missing(tmp_path):
    path = write(tmp_path, "# Just a heading\n\nNo frontmatter here.\n")

    message = error_of(path)

    assert str(path) in message
    assert "missing" in message


def test_file_not_starting_with_delimiter_is_missing_frontmatter(tmp_path):
    path = write(tmp_path, "\n" + role_text())

    message = error_of(path)

    assert str(path) in message
    assert "missing" in message


def test_empty_file_says_frontmatter_is_missing(tmp_path):
    path = write(tmp_path, "")

    message = error_of(path)

    assert str(path) in message
    assert "missing" in message


def test_frontmatter_never_closed_says_unterminated(tmp_path):
    path = write(tmp_path, "---\nname: product_manager\ntitle: PM\n")

    message = error_of(path)

    assert str(path) in message
    assert "unterminated" in message


@pytest.mark.parametrize("frontmatter", ["- one\n- two\n", "", "just a string\n"])
def test_frontmatter_that_is_not_a_mapping_names_file(tmp_path, frontmatter):
    path = write(tmp_path, f"---\n{frontmatter}---\nBody\n")

    message = error_of(path)

    assert str(path) in message
    assert "mapping" in message


def test_non_string_frontmatter_key_names_file(tmp_path):
    path = write(tmp_path, role_text(extra="1: one\n"))

    message = error_of(path)

    assert str(path) in message
    assert "strings" in message


def test_file_that_is_not_utf8_names_file(tmp_path):
    path = write(tmp_path, role_text().encode("utf-8") + b"\xff\xfe broken")

    message = error_of(path)

    assert str(path) in message
    assert "UTF-8" in message


def test_unsafe_yaml_tag_is_an_error_and_executes_nothing(tmp_path):
    marker = tmp_path / "pwned"
    unsafe = f'!!python/object/apply:os.system ["touch {marker}"]'
    path = write(tmp_path, role_text(title=unsafe))

    message = error_of(path)

    assert str(path) in message
    assert not marker.exists()


@pytest.mark.parametrize("body", ["", "   ", "\n\n  \n"])
def test_empty_or_whitespace_body_is_an_error(tmp_path, body):
    path = write(tmp_path, role_text(body=body))

    message = error_of(path)

    assert str(path) in message
    assert "body" in message


def test_missing_file_raises_role_error_naming_path(tmp_path):
    path = tmp_path / "nope.md"

    assert str(path) in error_of(path)


def test_yaml_comment_in_frontmatter_is_ignored(tmp_path):
    role = load_role(write(tmp_path, role_text(extra="# see https://example.com/roles#x\n")))

    assert role.name == "product_manager"


# --- loading a directory ---


def test_load_roles_returns_roles_keyed_by_name(tmp_path):
    shutil.copy(VALID, tmp_path / "valid.md")

    roles = load_roles(tmp_path)

    assert list(roles) == ["product_manager"]
    assert roles["product_manager"] == load_role(VALID)


def test_adding_a_role_file_adds_a_role_without_code_changes(tmp_path):
    shutil.copy(VALID, tmp_path / "product_manager_file.md")
    shutil.copy(FIXTURES / "qa_engineer.md", tmp_path / "second_role_file.md")

    roles = load_roles(tmp_path)

    assert set(roles) == {"product_manager", "qa_engineer"}
    assert roles["qa_engineer"].title == "Quality Assurance Engineer"


def test_fixtures_directory_loads_as_a_whole():
    roles = load_roles(FIXTURES)

    assert set(roles) == {"product_manager", "qa_engineer"}


def test_load_roles_orders_by_file_name_not_role_name(tmp_path):
    write(tmp_path, role_text(name="zeta"), name="a.md")
    write(tmp_path, role_text(name="alpha"), name="b.md")
    write(tmp_path, role_text(name="mid"), name="c.md")

    assert list(load_roles(tmp_path)) == ["zeta", "alpha", "mid"]


def test_load_roles_ignores_other_extensions_dotfiles_and_subdirectories(tmp_path):
    write(tmp_path, role_text(), name="good.md")
    write(tmp_path, "not a role", name="notes.txt")
    write(tmp_path, "a: b", name="roles.yaml")
    write(tmp_path, "garbage", name=".hidden.md")
    (tmp_path / "sub").mkdir()
    write(tmp_path / "sub", "garbage", name="nested.md")
    (tmp_path / "dir.md").mkdir()

    assert list(load_roles(tmp_path)) == ["product_manager"]


def test_readme_in_roles_directory_must_be_a_valid_role(tmp_path):
    write(tmp_path, role_text(), name="good.md")
    readme = write(tmp_path, "# Roles\n\nThis directory holds roles.\n", name="README.md")

    with pytest.raises(RoleError) as info:
        load_roles(tmp_path)

    assert str(readme) in str(info.value)


def test_duplicate_role_name_names_both_files_and_the_name(tmp_path):
    first = write(tmp_path, role_text(), name="a.md")
    second = write(tmp_path, role_text(), name="b.md")

    with pytest.raises(RoleError) as info:
        load_roles(tmp_path)

    message = str(info.value)
    assert str(first) in message
    assert str(second) in message
    assert "product_manager" in message


def test_all_invalid_files_are_reported_in_one_error(tmp_path):
    good = write(tmp_path, role_text(), name="a_good.md")
    bad_field = write(tmp_path, role_text(title="123", name="one"), name="b_bad.md")
    bad_yaml = write(tmp_path, "---\ntitle: [x\n---\nBody\n", name="c_bad.md")
    no_matter = write(tmp_path, "no frontmatter", name="d_bad.md")

    with pytest.raises(RoleError) as info:
        load_roles(tmp_path)

    message = str(info.value)
    assert str(good) not in message
    assert f"{bad_field}: title" in message
    assert f"{bad_yaml}: invalid YAML" in message
    assert f"{no_matter}: frontmatter is missing" in message


def test_invalid_file_and_duplicate_are_reported_together(tmp_path):
    write(tmp_path, role_text(), name="a.md")
    write(tmp_path, role_text(), name="b.md")
    bad = write(tmp_path, "nothing", name="c.md")

    with pytest.raises(RoleError) as info:
        load_roles(tmp_path)

    message = str(info.value)
    assert str(bad) in message
    assert "duplicate" in message


def test_directory_without_md_files_returns_empty_dict(tmp_path):
    write(tmp_path, "hello", name="notes.txt")

    assert load_roles(tmp_path) == {}


def test_missing_directory_raises_role_error_naming_path(tmp_path):
    missing = tmp_path / "roles"

    with pytest.raises(RoleError) as info:
        load_roles(missing)

    assert str(missing) in str(info.value)


def test_path_that_is_a_file_raises_role_error_naming_path():
    with pytest.raises(RoleError) as info:
        load_roles(VALID)

    assert str(VALID) in str(info.value)
