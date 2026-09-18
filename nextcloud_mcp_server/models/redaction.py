"""Redaction metadata carried on responses whose text was redacted (ADR-038)."""

from pydantic import BaseModel, Field


class RedactionInfo(BaseModel):
    """What a redacted response did to its text."""

    applied: bool = Field(
        description=(
            "True when person names in this response were replaced with "
            "`[PERSON_n]`. The same person has the same number across every "
            "field of the response."
        )
    )
    persons_redacted: int = Field(
        description="Distinct person names replaced (a bare surname counts apart)"
    )
    kept_names: list[str] = Field(
        default_factory=list,
        description="The caller's keep_names, which were left as written",
    )
    detector_model: str | None = Field(
        None, description="NER model that detected the names"
    )
