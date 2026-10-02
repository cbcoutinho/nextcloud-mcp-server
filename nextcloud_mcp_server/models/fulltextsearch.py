"""Pydantic models for FullTextSearch responses."""

from pydantic import BaseModel, Field

from .base import BaseResponse


class FullTextSearchHit(BaseModel):
    """One result returned by the Nextcloud FullTextSearch app.

    The upstream app groups hits by the search *provider* that produced them
    and serialises each entry loosely, so several fields are best-effort: a
    provider that indexes a document type it owns may not expose a filesystem
    ``path``, only a browser ``url`` or a provider-specific ``reference``.
    """

    provider: str = Field(
        description="FullTextSearch provider id that produced this hit "
        "(e.g. 'files', 'deck', 'activity').",
    )
    title: str = Field(
        default="",
        description="Result title — usually the file name or document title.",
    )
    path: str | None = Field(
        default=None,
        description=(
            "Relative path of the matching file, when the provider exposed one. "
            "Feed it straight to the WebDAV read/download tools. None when the "
            "hit is not addressable as a file (e.g. an activity or comment entry)."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Browser link that opens the matching item in Nextcloud, when the "
            "provider supplied one. Offer it so the user can open the result in place."
        ),
    )
    subtitle: str | None = Field(
        default=None,
        description="Secondary info line from the provider (author, location, snippet).",
    )
    score: float | None = Field(
        default=None,
        description="Relevance score, when the provider returned one.",
    )
    reference: str | None = Field(
        default=None,
        description="Provider-specific id/reference for the matched item.",
    )
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Raw provider attributes (mime, ext, mtime, size, …) carried through "
            "verbatim for callers that need fields this model does not promote."
        ),
    )


class FullTextSearchResponse(BaseResponse):
    """Response model for FullTextSearch-backed file discovery."""

    query: str = Field(description="The search term that was sent.")
    results: list[FullTextSearchHit] = Field(
        default_factory=list,
        description="Matching entries, flattened across providers.",
    )
    total_found: int = Field(
        description="Number of entries returned in this response.",
    )
    providers: list[str] = Field(
        default_factory=list,
        description="Provider ids the request was limited to (empty = all configured).",
    )
    note: str | None = Field(
        default=None,
        description=(
            "Operator-facing context when the result set is empty or degraded — "
            "for example that no search platform/index is configured. Report it "
            "to the user verbatim when present."
        ),
    )
