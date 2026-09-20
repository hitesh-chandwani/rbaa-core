import pytest
from pydantic import ValidationError

from rbaa.roles import Role

FIELDS = {
    "name": "product_manager",
    "title": "Product Manager",
    "responsibilities": ["Own the backlog"],
    "communication_style": "Concise",
    "standup_max_seconds": 75,
    "default_voice": "Kore",
    "tool_permissions": ["github:read"],
    "guidelines": "Be brief.",
}


def test_role_can_be_built_from_its_fields():
    assert Role(**FIELDS).name == "product_manager"


def test_role_rejects_a_string_for_standup_max_seconds():
    with pytest.raises(ValidationError):
        Role(**{**FIELDS, "standup_max_seconds": "90"})


def test_role_rejects_an_unknown_field():
    with pytest.raises(ValidationError):
        Role(**FIELDS, extra_field="x")


def test_role_is_frozen():
    with pytest.raises(ValidationError):
        Role(**FIELDS).name = "other"
