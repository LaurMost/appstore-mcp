import pytest

from appstore_mcp.apple.ids import AppRef, parse_app_ref
from appstore_mcp.errors import InvalidInputError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("570060128", AppRef(app_id="570060128", country=None)),
        (
            "https://apps.apple.com/us/app/duolingo-language-lessons/id570060128",
            AppRef(app_id="570060128", country="us"),
        ),
        (
            "https://apps.apple.com/app/id570060128",
            AppRef(app_id="570060128", country=None),
        ),
    ],
)
def test_parses_valid_app_refs(value: str, expected: AppRef) -> None:
    assert parse_app_ref(value) == expected


def test_unparseable_input_raises_with_guidance() -> None:
    with pytest.raises(InvalidInputError, match="570060128"):
        parse_app_ref("duolingo")
