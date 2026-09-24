"""Models for SAR export archives (ADR-040)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .base import BaseResponse

MAX_ITEMS = 500
MAX_KEEP = 50
MAX_QUERIES = 200


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
