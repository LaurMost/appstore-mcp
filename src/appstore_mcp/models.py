"""Pydantic models: normalized shapes plus per-tool result envelopes.

Compact-by-default: these models ARE the tool output. Raw Apple payloads are
returned only when explicitly requested via include_raw.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Meta(BaseModel):
    store: Literal["apple_app_store"] = "apple_app_store"
    country: str
    retrieved_at: datetime = Field(default_factory=utcnow)
    fresh: bool = True
    warnings: list[str] = Field(default_factory=list)


class Source(BaseModel):
    name: str
    url: str
    retrieved_at: datetime = Field(default_factory=utcnow)


class SearchResult(BaseModel):
    app_id: str
    name: str
    developer: str
    bundle_id: str | None = None
    url: str | None = None
    price: float | None = None
    currency: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    primary_genre: str | None = None


class AppProfile(SearchResult):
    description: str = ""
    release_notes: str | None = None
    version: str | None = None
    release_date: str | None = None
    last_updated: str | None = None
    min_os_version: str | None = None
    content_rating: str | None = None
    genres: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    device_families: list[str] = Field(default_factory=list)
    icon_url: str | None = None
    screenshot_urls: list[str] = Field(default_factory=list)
    ipad_screenshot_count: int = 0
    # Page-sourced fields (best-effort enrichment; null when unavailable).
    subtitle: str | None = None
    has_iap: bool | None = None
    privacy: list[dict[str, Any]] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def description_length(self) -> int:
        return len(self.description)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def screenshot_count(self) -> int:
        return len(self.screenshot_urls)


class Review(BaseModel):
    review_id: str | None = None
    title: str | None = None
    body: str
    rating: int | None = None
    author: str | None = None
    app_version: str | None = None
    updated_at: str | None = None
    vote_count: int | None = None
    vote_sum: int | None = None


class ChartEntry(BaseModel):
    rank: int
    app_id: str
    name: str
    developer: str | None = None
    bundle_id: str | None = None
    url: str | None = None
    price_label: str | None = None
    category: str | None = None
    release_date: str | None = None
    icon_url: str | None = None


class AppError(BaseModel):
    """Per-item failure inside an otherwise successful batch response."""

    app: str
    reason: str


class SearchAppsResult(BaseModel):
    meta: Meta
    query: str
    results: list[SearchResult]
    sources: list[Source]


class GetAppResult(BaseModel):
    meta: Meta
    app: AppProfile
    sources: list[Source]
    raw: dict[str, Any] | None = None


class CompareAppsResult(BaseModel):
    meta: Meta
    apps: list[AppProfile]
    errors: list[AppError] = Field(default_factory=list)
    sources: list[Source]


class ReviewsResult(BaseModel):
    meta: Meta
    app_id: str
    reviews: list[Review]
    sources: list[Source]


class ChartsResult(BaseModel):
    meta: Meta
    chart: str
    category: str | None = None
    entries: list[ChartEntry]
    sources: list[Source]
