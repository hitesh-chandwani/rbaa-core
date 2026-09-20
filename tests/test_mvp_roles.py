"""The three MVP role files in `roles/` (issue #4): they load, differ, and carry the same rules."""

import re
from itertools import combinations
from pathlib import Path

import pytest

from rbaa.roles import Role, load_roles

ROLES_DIR = Path(__file__).resolve().parents[1] / "roles"

TITLES = {
    "product_manager": "Product Manager",
    "senior_software_engineer": "Senior Software Engineer",
    "qa_engineer": "QA Engineer",
}
ROLE_NAMES = list(TITLES)

# Gemini prebuilt voice names, copied from the "Voice options" list at
# https://ai.google.dev/gemini-api/docs/speech-generation#voices (checked 2026-09-20; 30 names).
# Nothing offline detects Google adding or removing a voice, so this is a snapshot.
GEMINI_VOICES = frozenset(
    {
        "Zephyr",
        "Puck",
        "Charon",
        "Kore",
        "Fenrir",
        "Leda",
        "Orus",
        "Aoede",
        "Callirrhoe",
        "Autonoe",
        "Enceladus",
        "Iapetus",
        "Umbriel",
        "Algieba",
        "Despina",
        "Erinome",
        "Algenib",
        "Rasalgethi",
        "Laomedeia",
        "Achernar",
        "Alnilam",
        "Schedar",
        "Gacrux",
        "Pulcherrima",
        "Achird",
        "Zubenelgenubi",
        "Vindemiatrix",
        "Sadachbia",
        "Sadaltager",
        "Sulafat",
    }
)
VOICE_DOCS_URL = "https://ai.google.dev/gemini-api/docs/speech-generation#voices"

CLAIM_RULE = "Only claim work as completed if you can cite a ticket or commit reference for it."
MISSING_RULE = "If information is missing, say that it is missing. Never guess."


def collapse(text: str) -> str:
    """Lowercase and collapse whitespace, so wrapping and capitalisation do not matter."""
    return " ".join(text.lower().split())


def flatten(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace: a punctuation tweak is no difference."""
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


@pytest.fixture(scope="module")
def roles() -> dict[str, Role]:
    return load_roles(ROLES_DIR)


def frontmatter_lines(name: str) -> list[str]:
    """The raw lines between the opening and closing `---` of a role file."""
    lines = (ROLES_DIR / f"{name}.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    closing = lines.index("---", 1)
    return lines[1:closing]


def test_roles_directory_holds_exactly_the_three_role_files():
    assert {p.name for p in ROLES_DIR.glob("*.md")} == {f"{name}.md" for name in ROLE_NAMES}


def test_load_roles_returns_exactly_the_three_roles(roles):
    assert set(roles) == set(ROLE_NAMES)


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_role_name_equals_its_file_name(name):
    text = (ROLES_DIR / f"{name}.md").read_text(encoding="utf-8")
    assert re.search(rf"^name: {name}$", text, re.MULTILINE)


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_file_is_utf8_and_frontmatter_starts_on_the_first_line(name):
    raw = (ROLES_DIR / f"{name}.md").read_bytes()
    raw.decode("utf-8")
    assert raw.startswith(b"---\n")


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_title_is_the_pinned_job_title(roles, name):
    assert roles[name].title == TITLES[name]


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_role_has_at_least_three_responsibilities(roles, name):
    assert len(roles[name].responsibilities) >= 3


def test_no_responsibility_appears_in_two_roles(roles):
    for a, b in combinations(ROLE_NAMES, 2):
        shared = {collapse(r) for r in roles[a].responsibilities} & {
            collapse(r) for r in roles[b].responsibilities
        }
        assert not shared, f"{a} and {b} share responsibilities: {shared}"


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_standup_max_seconds_is_an_integer_from_60_to_90(roles, name):
    seconds = roles[name].standup_max_seconds
    assert isinstance(seconds, int)
    assert 60 <= seconds <= 90


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_tool_permissions_are_github_read_then_jira_read(roles, name):
    assert roles[name].tool_permissions == ["github:read", "jira:read"]


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_default_voice_is_on_the_gemini_voice_list(roles, name):
    assert roles[name].default_voice in GEMINI_VOICES


def test_default_voices_are_pairwise_different(roles):
    voices = [roles[name].default_voice for name in ROLE_NAMES]
    assert len(set(voices)) == len(voices), voices


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_frontmatter_comment_cites_the_voice_docs(name):
    comments = [line for line in frontmatter_lines(name) if line.lstrip().startswith("#")]
    assert any(VOICE_DOCS_URL in line for line in comments)


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_voice_docs_comment_is_not_part_of_the_body(roles, name):
    assert VOICE_DOCS_URL not in roles[name].guidelines


def test_communication_styles_are_pairwise_different(roles):
    for a, b in combinations(ROLE_NAMES, 2):
        assert flatten(roles[a].communication_style) != flatten(roles[b].communication_style), (
            f"{a} and {b} have the same communication_style"
        )


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_body_mentions_the_role_title(roles, name):
    assert TITLES[name].lower() in roles[name].guidelines.lower()


def test_no_two_bodies_are_equal(roles):
    bodies = [roles[name].guidelines for name in ROLE_NAMES]
    assert len(set(bodies)) == len(bodies)


@pytest.mark.parametrize("sentence", [CLAIM_RULE, MISSING_RULE], ids=["claim", "missing"])
@pytest.mark.parametrize("name", ROLE_NAMES)
def test_body_contains_anti_fabrication_sentence_verbatim(roles, name, sentence):
    assert collapse(sentence) in collapse(roles[name].guidelines)


@pytest.mark.parametrize("phrase", ["in progress", "open pull request"])
@pytest.mark.parametrize("name", ROLE_NAMES)
def test_body_says_in_progress_work_is_not_completed(roles, name, phrase):
    assert phrase in collapse(roles[name].guidelines)
