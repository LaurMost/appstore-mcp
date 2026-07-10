import pytest

from appstore_mcp.apple.ids import AppRef, parse_app_ref
from appstore_mcp.errors import InvalidInputError


def test_parses_plain_numeric_id() -> None:
    assert parse_app_ref("570060128") == AppRef(app_id="570060128", country=None)


def test_parses_app_store_url_with_id_and_country() -> None:
    ref = parse_app_ref(
        "https://apps.apple.com/us/app/duolingo-language-lessons/id570060128"
    )
    assert ref == AppRef(app_id="570060128", country="us")


def test_parses_url_without_country_segment() -> None:
    ref = parse_app_ref("https://apps.apple.com/app/id570060128")
    assert ref == AppRef(app_id="570060128", country=None)


def test_unparseable_input_raises_with_guidance() -> None:
    with pytest.raises(InvalidInputError, match="570060128"):
        parse_app_ref("duolingo")
