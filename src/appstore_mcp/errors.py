"""Error types whose messages are written for agent recovery.

All errors subclass fastmcp's ToolError so their messages pass through to the
client verbatim instead of being masked as internal server errors.
"""

from fastmcp.exceptions import ToolError


class AppStoreMCPError(ToolError):
    """Base for all appstore-mcp tool errors."""


class InvalidInputError(AppStoreMCPError):
    """The caller passed something we cannot interpret."""


class AppNotFoundError(AppStoreMCPError):
    def __init__(self, app_id: str, country: str) -> None:
        super().__init__(
            f"App {app_id} not found in storefront '{country}'. Apps are listed "
            f"per-country - it may exist in another storefront; try country='us' "
            f"or find the right ID with search_app_store."
        )


class RateLimitedError(AppStoreMCPError):
    def __init__(self, source: str) -> None:
        super().__init__(
            f"Apple rate-limited this request to {source}. Wait a minute and "
            f"retry; results are cached for ~15 minutes once fetched."
        )


class UpstreamError(AppStoreMCPError):
    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"Request to {source} failed: {detail}")
