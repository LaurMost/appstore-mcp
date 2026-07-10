"""Parse user-supplied app references: numeric IDs or App Store URLs."""

import re
from dataclasses import dataclass

from appstore_mcp.errors import InvalidInputError

_URL_RE = re.compile(
    r"https?://(?:apps|itunes)\.apple\.com/(?:([a-z]{2})/)?[^\s]*?id(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AppRef:
    app_id: str
    country: str | None


def parse_app_ref(value: str) -> AppRef:
    value = value.strip()
    if value.isdigit():
        return AppRef(app_id=value, country=None)
    match = _URL_RE.search(value)
    if match:
        country = match.group(1)
        return AppRef(app_id=match.group(2), country=country.lower() if country else None)
    raise InvalidInputError(
        f"Could not parse an App Store app ID from {value!r}. Pass a numeric ID "
        f"like '570060128' or a URL like "
        f"'https://apps.apple.com/us/app/duolingo-language-lessons/id570060128'."
    )
