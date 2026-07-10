from collections.abc import Callable
from typing import Any

import pytest

from appstore_mcp.apple.page import PageParseError, enrichment_from_html


def test_extracts_enrichment_fields_from_real_page(
    load_fixture: Callable[[str], Any],
) -> None:
    html = load_fixture("page_duolingo_us.html")
    enrichment = enrichment_from_html(html)
    assert enrichment.subtitle == "Languages, Math, Music & Chess"
    assert enrichment.has_iap is True
    assert enrichment.privacy is not None
    identifiers = {p["identifier"] for p in enrichment.privacy}
    assert "DATA_USED_TO_TRACK_YOU" in identifiers
    tracked = next(
        p for p in enrichment.privacy if p["identifier"] == "DATA_USED_TO_TRACK_YOU"
    )
    assert "Purchases" in tracked["categories"]
    # Compact: no artwork/styling noise in the normalized privacy cards.
    assert "artwork" not in tracked


def test_page_without_embedded_data_raises() -> None:
    with pytest.raises(PageParseError):
        enrichment_from_html("<html><body>nothing here</body></html>")
