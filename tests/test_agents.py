import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from rbaa.agents import Agent, AgentError, load_agent, load_agents
from rbaa.agents.models import AgentFrontmatter
from rbaa.roles import Role

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
EXAMPLE = AGENTS_DIR / "example.yaml"

# The fields of a valid agent, as raw YAML per field, so tests can change one field at a time.
FIELDS = {
    "agent_id": "devops_bot",
    "role": "product_manager",
    "display_name": "DevOps Bot",
    "github_username": "devops-bot",
    "jira_account_id": "acc-1",
    "github_token_env": "DEVOPS_GITHUB_TOKEN",
    "jira_token_env": "DEVOPS_JIRA_TOKEN",
    "allowed_repos": "[]",
    "allowed_jira_projects": "[]",
}

ROLES: dict[str, Role] = {
    "product_manager": Role(
        name="product_manager",
        title="Product Manager",
        responsibilities=["Own the backlog"],
        communication_style="Concise",
        standup_max_seconds=75,
        default_voice="Kore",
        tool_permissions=["github:read"],
        guidelines="Be brief.",
    ),
    "qa_engineer": Role(
        name="qa_engineer",
        title="QA Engineer",
        responsibilities=["Verify work"],
        communication_style="Precise",
        standup_max_seconds=60,
        default_voice="Puck",
        tool_permissions=[],
        guidelines="Report findings.",
    ),
}


def agent_text(**overrides: str | None) -> str:
    """An agent file's text. `overrides` replaces a field's raw YAML; None drops the field."""
    fields = {**FIELDS, **overrides}
    lines = [f"{key}: {value}" for key, value in fields.items() if value is not None]
    return "\n".join(lines) + "\n"


def write(tmp_path: Path, text: str | bytes, name: str = "agent.yaml") -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def error_of(path: Path) -> str:
    with pytest.raises(AgentError) as info:
        load_agent(path, ROLES)
    return str(info.value)


@pytest.fixture
def tokens_set(monkeypatch):
    """Set the two token environment variables the default `FIELDS` name, to a fake value."""
    monkeypatch.setenv(FIELDS["github_token_env"], "tok-fake-github")
    monkeypatch.setenv(FIELDS["jira_token_env"], "tok-fake-jira")


# --- loading a valid file ---


def test_valid_agent_loads_every_field(tmp_path, tokens_set):
    path = write(tmp_path, agent_text(allowed_repos="\n  - my-org/my-repo"))

    agent = load_agent(path, ROLES)

    assert isinstance(agent, Agent)
    assert agent.agent_id == "devops_bot"
    assert agent.role == "product_manager"
    assert agent.display_name == "DevOps Bot"
    assert agent.github_username == "devops-bot"
    assert agent.jira_account_id == "acc-1"
    assert agent.github_token_env == "DEVOPS_GITHUB_TOKEN"
    assert agent.jira_token_env == "DEVOPS_JIRA_TOKEN"
    assert agent.allowed_repos == ["my-org/my-repo"]
    assert agent.allowed_jira_projects == []


def test_loaded_agent_is_read_only(tmp_path, tokens_set):
    agent = load_agent(write(tmp_path, agent_text()), ROLES)

    with pytest.raises(ValueError):
        agent.display_name = "Someone Else"


def test_example_yaml_loads_with_env_vars_set_by_the_test(monkeypatch):
    monkeypatch.setenv("EXAMPLE_GITHUB_TOKEN", "tok-fake-example-gh")
    monkeypatch.setenv("EXAMPLE_JIRA_TOKEN", "tok-fake-example-jira")

    agent = load_agent(EXAMPLE, ROLES)

    assert agent.agent_id == "example_pm"
    assert agent.role == "product_manager"


def test_env_example_is_not_changed_by_this_feature():
    # Token variable names are chosen per agent in the YAML, not read by Settings; the app's
    # .env.example sync tests (tests/test_config.py) are unaffected and still pass unchanged.
    assert "EXAMPLE_GITHUB_TOKEN" not in (REPO_ROOT / ".env.example").read_text()
    assert "EXAMPLE_JIRA_TOKEN" not in (REPO_ROOT / ".env.example").read_text()


# --- field rules: errors on one file ---


@pytest.mark.parametrize("field", list(FIELDS))
def test_missing_required_field_names_file_and_field(tmp_path, field):
    path = write(tmp_path, agent_text(**{field: None}))

    message = error_of(path)

    assert str(path) in message
    assert field in message


@pytest.mark.parametrize("field", ["unknown_field", "github_token", "jira_token", "alowed_repos"])
def test_unknown_field_is_rejected_and_named(tmp_path, field):
    path = write(tmp_path, agent_text() + f"{field}: x\n")

    message = error_of(path)

    assert str(path) in message
    assert field in message


@pytest.mark.parametrize(
    "value", ["Devops_bot", "1bot", "-bot", "has space", "UPPER", "", "a" * 65]
)
def test_invalid_agent_id_names_file_and_field(tmp_path, value):
    path = write(tmp_path, agent_text(agent_id=f'"{value}"' if value else '""'))

    message = error_of(path)

    assert str(path) in message
    assert "agent_id" in message


def test_duplicate_agent_id_raises_naming_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv(FIELDS["github_token_env"], "tok-1")
    monkeypatch.setenv(FIELDS["jira_token_env"], "tok-2")
    first = write(tmp_path, agent_text(display_name="First"), name="a.yaml")
    second = write(tmp_path, agent_text(display_name="Second"), name="b.yaml")

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert str(first) in message
    assert str(second) in message
    assert "agent_id" in message


def test_unknown_role_names_agent_role_and_known_roles(tmp_path):
    path = write(tmp_path, agent_text(role="does_not_exist"))

    message = error_of(path)

    assert str(path) in message
    assert "devops_bot" in message
    assert "does_not_exist" in message
    assert "product_manager" in message
    assert "qa_engineer" in message


def test_role_name_match_is_case_sensitive(tmp_path):
    path = write(tmp_path, agent_text(role="Product_Manager"))

    message = error_of(path)

    assert "Product_Manager" in message


@pytest.mark.parametrize("value", ['""', '"   "'])
def test_empty_display_name_after_trim_names_file_and_field(tmp_path, value):
    path = write(tmp_path, agent_text(display_name=value))

    message = error_of(path)

    assert str(path) in message
    assert "display_name" in message


def test_duplicate_display_name_case_insensitive_after_trim_names_both_agents(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(FIELDS["github_token_env"], "tok-1")
    monkeypatch.setenv(FIELDS["jira_token_env"], "tok-2")
    first = write(
        tmp_path,
        agent_text(agent_id="agent_one", display_name="Sam"),
        name="a.yaml",
    )
    second = write(
        tmp_path,
        agent_text(agent_id="agent_two", display_name='"  sam  "'),
        name="b.yaml",
    )

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert "agent_one" in message
    assert "agent_two" in message
    assert str(first) in message
    assert str(second) in message


def test_two_agents_same_role_different_identity_are_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("A_GITHUB_TOKEN", "tok-1")
    monkeypatch.setenv("A_JIRA_TOKEN", "tok-2")
    monkeypatch.setenv("B_GITHUB_TOKEN", "tok-3")
    monkeypatch.setenv("B_JIRA_TOKEN", "tok-4")
    write(
        tmp_path,
        agent_text(
            agent_id="agent_one",
            display_name="One",
            github_username="one",
            jira_account_id="acc-one",
            github_token_env="A_GITHUB_TOKEN",
            jira_token_env="A_JIRA_TOKEN",
        ),
        name="a.yaml",
    )
    write(
        tmp_path,
        agent_text(
            agent_id="agent_two",
            display_name="Two",
            github_username="two",
            jira_account_id="acc-two",
            github_token_env="B_GITHUB_TOKEN",
            jira_token_env="B_JIRA_TOKEN",
        ),
        name="b.yaml",
    )

    agents = load_agents(tmp_path, ROLES)

    assert set(agents) == {"agent_one", "agent_two"}
    assert agents["agent_one"].role == agents["agent_two"].role == "product_manager"


@pytest.mark.parametrize("field", ["github_username", "jira_account_id"])
@pytest.mark.parametrize("value", ['""', '"   "'])
def test_empty_identity_field_names_file_and_field(tmp_path, field, value):
    path = write(tmp_path, agent_text(**{field: value}))

    message = error_of(path)

    assert str(path) in message
    assert field in message


def test_duplicate_github_username_case_insensitive_names_both_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("A_GITHUB_TOKEN", "tok-1")
    monkeypatch.setenv("A_JIRA_TOKEN", "tok-2")
    monkeypatch.setenv("B_GITHUB_TOKEN", "tok-3")
    monkeypatch.setenv("B_JIRA_TOKEN", "tok-4")
    first = write(
        tmp_path,
        agent_text(
            agent_id="agent_one",
            display_name="One",
            github_username="Devops-Bot",
            jira_account_id="acc-one",
            github_token_env="A_GITHUB_TOKEN",
            jira_token_env="A_JIRA_TOKEN",
        ),
        name="a.yaml",
    )
    second = write(
        tmp_path,
        agent_text(
            agent_id="agent_two",
            display_name="Two",
            github_username="devops-bot",
            jira_account_id="acc-two",
            github_token_env="B_GITHUB_TOKEN",
            jira_token_env="B_JIRA_TOKEN",
        ),
        name="b.yaml",
    )

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert "agent_one" in message
    assert "agent_two" in message
    assert str(first) in message
    assert str(second) in message


def test_duplicate_jira_account_id_names_both_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("A_GITHUB_TOKEN", "tok-1")
    monkeypatch.setenv("A_JIRA_TOKEN", "tok-2")
    monkeypatch.setenv("B_GITHUB_TOKEN", "tok-3")
    monkeypatch.setenv("B_JIRA_TOKEN", "tok-4")
    write(
        tmp_path,
        agent_text(
            agent_id="agent_one",
            display_name="One",
            github_username="one",
            jira_account_id="acc-shared",
            github_token_env="A_GITHUB_TOKEN",
            jira_token_env="A_JIRA_TOKEN",
        ),
        name="a.yaml",
    )
    write(
        tmp_path,
        agent_text(
            agent_id="agent_two",
            display_name="Two",
            github_username="two",
            jira_account_id="acc-shared",
            github_token_env="B_GITHUB_TOKEN",
            jira_token_env="B_JIRA_TOKEN",
        ),
        name="b.yaml",
    )

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert "agent_one" in message
    assert "agent_two" in message
    assert "acc-shared" in message


@pytest.mark.parametrize("value", ["1BAD", "has-dash", "has space", "has.dot"])
def test_invalid_token_env_var_name_names_file_and_field(tmp_path, value):
    path = write(tmp_path, agent_text(github_token_env=f'"{value}"'))

    message = error_of(path)

    assert str(path) in message
    assert "github_token_env" in message


def test_empty_token_env_var_name_names_file_and_field(tmp_path):
    path = write(tmp_path, agent_text(github_token_env='""'))

    message = error_of(path)

    assert str(path) in message
    assert "github_token_env" in message


def test_token_env_validation_error_never_echoes_a_pasted_token_value(tmp_path):
    sneaky = "ghp-FAKE1234-not-a-real-token"
    path = write(tmp_path, agent_text(github_token_env=f'"{sneaky}"'))

    message = error_of(path)

    assert sneaky not in message
    assert "github_token_env" in message


def test_duplicate_github_token_env_across_agents_is_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_TOKEN", "tok-1")
    monkeypatch.setenv("A_JIRA_TOKEN", "tok-2")
    monkeypatch.setenv("B_JIRA_TOKEN", "tok-3")
    write(
        tmp_path,
        agent_text(
            agent_id="agent_one",
            display_name="One",
            github_username="one",
            jira_account_id="acc-one",
            github_token_env="SHARED_TOKEN",
            jira_token_env="A_JIRA_TOKEN",
        ),
        name="a.yaml",
    )
    write(
        tmp_path,
        agent_text(
            agent_id="agent_two",
            display_name="Two",
            github_username="two",
            jira_account_id="acc-two",
            github_token_env="SHARED_TOKEN",
            jira_token_env="B_JIRA_TOKEN",
        ),
        name="b.yaml",
    )

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert "agent_one" in message
    assert "agent_two" in message
    assert "SHARED_TOKEN" in message


def test_duplicate_token_env_across_different_fields_is_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_TOKEN", "tok-1")
    monkeypatch.setenv("A_GITHUB_TOKEN", "tok-2")
    monkeypatch.setenv("B_JIRA_TOKEN", "tok-3")
    write(
        tmp_path,
        agent_text(
            agent_id="agent_one",
            display_name="One",
            github_username="one",
            jira_account_id="acc-one",
            github_token_env="A_GITHUB_TOKEN",
            jira_token_env="SHARED_TOKEN",
        ),
        name="a.yaml",
    )
    write(
        tmp_path,
        agent_text(
            agent_id="agent_two",
            display_name="Two",
            github_username="two",
            jira_account_id="acc-two",
            github_token_env="SHARED_TOKEN",
            jira_token_env="B_JIRA_TOKEN",
        ),
        name="b.yaml",
    )

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert "agent_one" in message
    assert "agent_two" in message
    assert "SHARED_TOKEN" in message


@pytest.mark.parametrize(
    "value",
    ['"just-a-name"', '"owner/name/extra"', '"/name"', '"owner/"', '"owner name/repo"'],
)
def test_invalid_allowed_repos_entry_names_file_and_field(tmp_path, value):
    path = write(tmp_path, agent_text(allowed_repos=f"[{value}]"))

    message = error_of(path)

    assert str(path) in message
    assert "allowed_repos" in message


@pytest.mark.parametrize("value", ['"lower"', '"1ABC"', '"A"', '""'])
def test_invalid_allowed_jira_projects_entry_names_file_and_field(tmp_path, value):
    path = write(tmp_path, agent_text(allowed_jira_projects=f"[{value}]"))

    message = error_of(path)

    assert str(path) in message
    assert "allowed_jira_projects" in message


def test_duplicate_allowed_repos_entry_is_error(tmp_path):
    path = write(
        tmp_path,
        agent_text(allowed_repos='["my-org/repo", "my-org/repo"]'),
    )

    message = error_of(path)

    assert str(path) in message
    assert "allowed_repos" in message
    assert "duplicate" in message


def test_duplicate_allowed_jira_projects_entry_is_error(tmp_path):
    path = write(tmp_path, agent_text(allowed_jira_projects='["OPS", "OPS"]'))

    message = error_of(path)

    assert str(path) in message
    assert "allowed_jira_projects" in message
    assert "duplicate" in message


def test_empty_allowed_lists_are_valid(tmp_path, tokens_set):
    agent = load_agent(
        write(tmp_path, agent_text(allowed_repos="[]", allowed_jira_projects="[]")), ROLES
    )

    assert agent.allowed_repos == []
    assert agent.allowed_jira_projects == []


@pytest.mark.parametrize("field", ["allowed_repos", "allowed_jira_projects"])
def test_allowed_list_field_as_a_string_is_error(tmp_path, field):
    path = write(tmp_path, agent_text(**{field: '"my-org/repo"'}))

    message = error_of(path)

    assert str(path) in message
    assert field in message


# --- errors reading the file itself ---


def test_empty_file_raises(tmp_path):
    path = write(tmp_path, "")

    message = error_of(path)

    assert str(path) in message
    assert "empty" in message


@pytest.mark.parametrize("text", ["- one\n- two\n", "just a string\n", "123\n"])
def test_file_that_is_not_a_mapping_raises(tmp_path, text):
    path = write(tmp_path, text)

    message = error_of(path)

    assert str(path) in message
    assert "mapping" in message


def test_malformed_yaml_names_file_and_line_number(tmp_path):
    text = "agent_id: devops_bot\nrole: [unclosed\ndisplay_name: DevOps Bot\n"
    path = write(tmp_path, text)

    message = error_of(path)

    assert str(path) in message
    assert "line 3" in message  # the parser reports the problem where the flow list breaks


def test_file_that_is_not_utf8_names_file(tmp_path):
    path = write(tmp_path, agent_text().encode("utf-8") + b"\xff\xfe broken")

    message = error_of(path)

    assert str(path) in message
    assert "UTF-8" in message


def test_missing_file_raises_agent_error_naming_path(tmp_path):
    path = tmp_path / "nope.yaml"

    assert str(path) in error_of(path)


# --- loading a directory ---


def test_load_agents_returns_agents_keyed_by_agent_id(tmp_path, tokens_set):
    write(tmp_path, agent_text())

    agents = load_agents(tmp_path, ROLES)

    assert list(agents) == ["devops_bot"]


def test_load_agents_ignores_other_extensions_and_dotfiles(tmp_path, tokens_set):
    write(tmp_path, agent_text(), name="good.yaml")
    write(tmp_path, "not an agent", name="notes.txt")
    write(tmp_path, "garbage", name=".hidden.yaml")
    write(tmp_path, "a: b", name="roles.yml")

    assert list(load_agents(tmp_path, ROLES)) == ["devops_bot"]


def test_all_invalid_files_are_reported_in_one_error(tmp_path):
    bad_field = write(tmp_path, agent_text(agent_id="ONE"), name="a_bad.yaml")
    bad_yaml = write(tmp_path, "agent_id: [x\n", name="b_bad.yaml")
    no_mapping = write(tmp_path, "just text", name="c_bad.yaml")

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert f"{bad_field}: agent_id" in message
    assert f"{bad_yaml}: invalid YAML" in message
    assert f"{no_mapping}: file must be a key/value mapping" in message


def test_directory_without_yaml_files_returns_empty_dict(tmp_path):
    write(tmp_path, "hello", name="notes.txt")

    assert load_agents(tmp_path, ROLES) == {}


def test_missing_directory_raises_agent_error_naming_path(tmp_path):
    missing = tmp_path / "agents"

    with pytest.raises(AgentError) as info:
        load_agents(missing, ROLES)

    assert str(missing) in str(info.value)


def test_path_that_is_a_file_raises_agent_error_naming_path(tmp_path):
    path = write(tmp_path, agent_text())

    with pytest.raises(AgentError) as info:
        load_agents(path, ROLES)

    assert str(path) in str(info.value)


# --- token handling ---


def test_token_is_read_from_environment_and_held_as_secretstr(tmp_path, monkeypatch):
    monkeypatch.setenv(FIELDS["github_token_env"], "tok-github-value")
    monkeypatch.setenv(FIELDS["jira_token_env"], "tok-jira-value")

    agent = load_agent(write(tmp_path, agent_text()), ROLES)

    assert isinstance(agent.github_token, SecretStr)
    assert isinstance(agent.jira_token, SecretStr)
    assert agent.github_token.get_secret_value() == "tok-github-value"
    assert agent.jira_token.get_secret_value() == "tok-jira-value"


@pytest.mark.parametrize("missing_field", ["github_token_env", "jira_token_env"])
def test_unset_token_env_var_raises_naming_variable_and_agent(tmp_path, monkeypatch, missing_field):
    other_field = "jira_token_env" if missing_field == "github_token_env" else "github_token_env"
    monkeypatch.setenv(FIELDS[other_field], "tok-set")
    monkeypatch.delenv(FIELDS[missing_field], raising=False)

    message = error_of(write(tmp_path, agent_text()))

    assert FIELDS[missing_field] in message
    assert "devops_bot" in message


def test_empty_token_env_var_value_is_treated_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv(FIELDS["github_token_env"], "")
    monkeypatch.setenv(FIELDS["jira_token_env"], "tok-set")

    message = error_of(write(tmp_path, agent_text()))

    assert FIELDS["github_token_env"] in message


def test_multiple_unset_tokens_across_agents_reported_together(tmp_path, monkeypatch):
    monkeypatch.delenv("A_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("A_JIRA_TOKEN", raising=False)
    monkeypatch.delenv("B_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("B_JIRA_TOKEN", raising=False)
    write(
        tmp_path,
        agent_text(
            agent_id="agent_one",
            display_name="One",
            github_username="one",
            jira_account_id="acc-one",
            github_token_env="A_GITHUB_TOKEN",
            jira_token_env="A_JIRA_TOKEN",
        ),
        name="a.yaml",
    )
    write(
        tmp_path,
        agent_text(
            agent_id="agent_two",
            display_name="Two",
            github_username="two",
            jira_account_id="acc-two",
            github_token_env="B_GITHUB_TOKEN",
            jira_token_env="B_JIRA_TOKEN",
        ),
        name="b.yaml",
    )

    with pytest.raises(AgentError) as info:
        load_agents(tmp_path, ROLES)

    message = str(info.value)
    assert "A_GITHUB_TOKEN" in message
    assert "A_JIRA_TOKEN" in message
    assert "B_GITHUB_TOKEN" in message
    assert "B_JIRA_TOKEN" in message


def test_known_token_value_never_appears_in_repr_str_dump_or_json(tmp_path, monkeypatch):
    token = "tok-SECRET-123"
    monkeypatch.setenv(FIELDS["github_token_env"], token)
    monkeypatch.setenv(FIELDS["jira_token_env"], token)

    agent = load_agent(write(tmp_path, agent_text()), ROLES)

    assert token not in repr(agent)
    assert token not in str(agent)
    assert token not in str(agent.model_dump())
    assert token not in agent.model_dump_json()


def test_known_token_value_never_appears_in_debug_logs(tmp_path, monkeypatch, caplog):
    token = "tok-SECRET-123"
    monkeypatch.setenv(FIELDS["github_token_env"], token)
    monkeypatch.setenv(FIELDS["jira_token_env"], token)

    with caplog.at_level("DEBUG"):
        load_agent(write(tmp_path, agent_text()), ROLES)

    assert all(token not in record.getMessage() for record in caplog.records)


def test_known_token_value_never_appears_in_error_message_from_an_unrelated_failure(
    tmp_path, monkeypatch
):
    token = "tok-SECRET-123"
    monkeypatch.setenv(FIELDS["github_token_env"], token)
    monkeypatch.setenv(FIELDS["jira_token_env"], token)
    path = write(tmp_path, agent_text(role="does_not_exist"))

    message = error_of(path)

    assert token not in message
    assert "does_not_exist" in message


def test_no_yaml_file_in_agents_directory_contains_a_token_value():
    for path in AGENTS_DIR.glob("*.yaml"):
        text = path.read_text()
        assert not re.search(r"^\s*(github|jira)_token\s*:", text, re.MULTILINE)


def test_agent_frontmatter_has_no_field_that_accepts_a_token_directly(tmp_path):
    assert "github_token" not in AgentFrontmatter.model_fields
    assert "jira_token" not in AgentFrontmatter.model_fields

    path = write(tmp_path, agent_text() + 'github_token: "tok-should-be-rejected"\n')
    message = error_of(path)

    assert "github_token" in message
    assert "tok-should-be-rejected" not in message
