"""Models for SAR export archives (ADR-040)."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..search.access_filter import MAX_PATH_PREFIXES
from .base import BaseResponse

MAX_KEEP = 50
MAX_QUERIES = 200
MAX_ITEMS_PER_CALL = 1000
# ponytail: a case is one JSON file rewritten on each change, and an export
# holds every document's text in memory. Fine for a few thousand documents;
# move cases to a database table and stream the export past that.
MAX_CASE_ITEMS = 2000
# Searches logged in one case. Repeats of a logged search are not logged again.
MAX_CASE_QUERIES = 1000


class SarItem(BaseModel):
    """One document to include in the archive."""

    doc_type: str = Field(
        description='Document type as returned by search, e.g. "file", "note".'
    )
    doc_id: str = Field(description="Document id as returned by search (`id`).")
    reason: str = Field(
        max_length=2000,
        description="Why this document is included. Redacted like the documents.",
    )
    page_start: int | None = Field(
        default=None, ge=1, description="First page to include (paged documents)."
    )
    page_end: int | None = Field(
        default=None, ge=1, description="Last page to include (paged documents)."
    )

    @field_validator("doc_id", mode="before")
    @classmethod
    def _stringify_id(cls, value: object) -> object:
        # Search returns numeric ids; the index stores them as strings.
        return str(value) if isinstance(value, int) else value

    @model_validator(mode="after")
    def _check_pages(self) -> "SarItem":
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must be >= page_start")
        return self


Subject = Annotated[str, Field(min_length=1, max_length=200)]
Query = Annotated[str, Field(min_length=1, max_length=10000)]
SubjectList = Annotated[list[Subject], Field(min_length=1, max_length=MAX_KEEP)]

CaseState = Literal["open", "exporting", "ready_for_audit", "closed"]


# --- The case file (sar-case.json) -------------------------------------------


class SarCaseItem(SarItem):
    """A document selected for the case."""

    title: str = Field(
        default="",
        max_length=500,
        description="Unredacted title, for the case only; never exported.",
    )
    found_by: str | None = Field(
        default=None, max_length=1000, description="The query that found it."
    )
    added_by: str = ""
    added_at: str = ""


class SarSearchFilters(BaseModel):
    """The filters a search ran with: the search API's own parameter names."""

    model_config = ConfigDict(extra="ignore")

    algorithm: str | None = Field(default=None, max_length=32)
    # Generous bounds: a search accepts any doc_types, and a log entry must not
    # refuse what the search took. Anything past them is refused before the
    # search runs, not after.
    doc_types: list[Annotated[str, Field(max_length=200)]] | None = Field(
        default=None, max_length=100
    )
    path_prefixes: list[Annotated[str, Field(max_length=1000)]] | None = Field(
        default=None, max_length=MAX_PATH_PREFIXES
    )
    modified_after: str | int | None = None
    modified_before: str | int | None = None
    score_threshold: float | None = None
    min_relevance: float | None = None
    fusion: str | None = Field(default=None, max_length=16)
    granularity: str | None = Field(default=None, max_length=16)
    rerank: bool | None = None

    def describe(self) -> str:
        """One line for the archive's search log; empty without filters."""
        parts = []
        if self.path_prefixes:
            parts.append("folders: " + ", ".join(self.path_prefixes))
        if self.doc_types:
            parts.append("types: " + ", ".join(self.doc_types))
        if self.modified_after is not None or self.modified_before is not None:
            parts.append(
                f"modified {self.modified_after or '…'} to {self.modified_before or '…'}"
            )
        if self.min_relevance:
            parts.append(f"min relevance {self.min_relevance}")
        if self.score_threshold:
            parts.append(f"score threshold {self.score_threshold}")
        if self.algorithm:
            parts.append(f"algorithm: {self.algorithm}")
        return "; ".join(parts)


class SarQueryLog(BaseModel):
    """A search run for the case, including ones that found nothing."""

    text: Query
    hits: int | None = Field(default=None, ge=0)
    filters: SarSearchFilters | None = None
    run_by: str = ""
    run_at: str = ""

    def describe(self) -> str:
        """The query as the archive lists it: its text, then its filters."""
        filters = self.filters.describe() if self.filters else ""
        return f"{self.text} ({filters})" if filters else self.text


class SarCaseExport(BaseModel):
    """One export of the case. Progress lives in its status file."""

    version: int
    state: Literal["running", "done", "failed"]
    archive_path: str
    status_path: str
    submitted_by: str
    submitted_at: str
    total: int
    failed: int = 0
    message: str | None = None


class SarCase(BaseModel):
    """A subject access request case, stored as ``sar-case.json`` in Nextcloud."""

    version: int = 1
    name: str
    description: str = Field(default="", max_length=2000)
    state: CaseState = "open"
    created_by: str
    created_at: str
    updated_at: str
    closed_by: str | None = None
    closed_at: str | None = None
    subject: SubjectList = Field(
        description="The data subject's names, aliases, emails, phone numbers, "
        "NI numbers and addresses. These are kept. Everyone else is redacted."
    )
    items: list[SarCaseItem] = Field(default_factory=list, max_length=MAX_CASE_ITEMS)
    queries: list[SarQueryLog] = Field(
        default_factory=list, max_length=MAX_CASE_QUERIES
    )
    exports: list[SarCaseExport] = Field(default_factory=list)
    recent_writes: list[str] = Field(
        default_factory=list,
        description="Internal: ids of the last writes, to detect lost updates.",
    )


# --- Requests (HTTP bodies; the MCP tools take the same fields) ----------------


class SarCaseCreate(BaseModel):
    folder: str = Field(
        max_length=1000,
        description="Existing folder to create the case in, e.g. a team folder. "
        "The case gets its own sub-folder named after it.",
    )
    name: str = Field(description='Case name, e.g. "SAR-2026-014".')
    subject: SubjectList
    description: str = Field(default="", max_length=2000)


class SarCaseUpdate(BaseModel):
    subject: SubjectList | None = None
    description: str | None = Field(default=None, max_length=2000)
    state: Literal["open", "closed"] | None = Field(
        default=None,
        description='"closed" finishes the case (read-only, final); "open" '
        "reopens a case that is ready for audit so it can be changed and "
        "exported again.",
    )


class SarItemRef(BaseModel):
    doc_type: str
    doc_id: str

    @field_validator("doc_id", mode="before")
    @classmethod
    def _stringify_id(cls, value: object) -> object:
        return str(value) if isinstance(value, int) else value


class SarQueryIn(BaseModel):
    text: Query
    hits: int | None = Field(default=None, ge=0)
    filters: SarSearchFilters | None = None


class SarCaseItemsChange(BaseModel):
    add: list[SarCaseItem] = Field(default_factory=list, max_length=MAX_ITEMS_PER_CALL)
    remove: list[SarItemRef] = Field(
        default_factory=list, max_length=MAX_ITEMS_PER_CALL
    )
    queries: list[SarQueryIn] = Field(default_factory=list, max_length=MAX_QUERIES)


class SarCaseExportRequest(BaseModel):
    output_folder: str | None = Field(
        default=None,
        max_length=1000,
        description="Where to write the archive. Defaults to the case's own "
        "exports/ folder.",
    )


class SarFailedItem(BaseModel):
    """A document that could not be exported. Ids only, never content."""

    doc_type: str
    doc_id: str
    error: str


class SarExportStatus(BaseResponse):
    """Progress of a SAR export, as recorded in its status file."""

    state: Literal["running", "done", "failed"] = Field(
        description="running, done (archive written) or failed."
    )
    archive_path: str = Field(description="Where the archive is (or will be).")
    status_path: str = Field(description="The status file next to the archive.")
    total: int = Field(description="Documents submitted.")
    processed: int = Field(description="Documents handled so far.")
    failed: int = Field(
        description="Documents that could not be exported; listed in the index."
    )
    failed_items: list[SarFailedItem] = Field(
        default_factory=list,
        description="Which documents failed and why (ids only). Kept out of the "
        "archive, whose index lists them by number.",
    )
    message: str | None = Field(
        default=None, description="Why the whole export failed, if it did."
    )
    started_at: str
    updated_at: str


# --- Responses -----------------------------------------------------------------


class SarCaseResponse(BaseResponse):
    """A case, with its items paged and its latest export's live progress."""

    case_id: int = Field(description="The case's id (its file's Nextcloud id).")
    path: str = Field(description="Where the case file is, for this user.")
    case: SarCase
    items_total: int = Field(description="Items in the case; `case.items` is a page.")
    items_offset: int = 0
    latest_export: SarExportStatus | None = Field(
        default=None, description="Progress of the most recent export, if any."
    )


class SarCaseSummary(BaseModel):
    case_id: int
    path: str
    name: str
    state: CaseState
    items: int
    updated_at: str
    latest_export: SarCaseExport | None = None


class SarCaseListResponse(BaseResponse):
    cases: list[SarCaseSummary]
