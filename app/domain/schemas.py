"""Domain schemas and errors.

Pure types with no I/O. This is the contract between the AI provider and
everything downstream of it (TR-2, H-4, M-10).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class QueryAnalysis(BaseModel):
    """Validated output of a single query-decomposition call.

    Replaces the previously unvalidated ``dict`` that was passed straight from
    ``json.loads`` to the formatter (H-4, M-10). Anything the provider omits or
    gets wrong is caught here, at the boundary, rather than crashing a task
    three layers downstream.
    """

    summary: str = Field(
        default="",
        description="One-line summary of the user's question.",
    )
    sub_questions: list[str] = Field(
        default_factory=list,
        description="Key sub-questions derived from the user's question.",
    )

    @field_validator("summary", mode="before")
    @classmethod
    def _coerce_summary(cls, value: object) -> str:
        """Tolerate a non-string summary rather than raising."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    @field_validator("sub_questions", mode="before")
    @classmethod
    def _coerce_sub_questions(cls, value: object) -> list[str]:
        """Normalise sub-questions to a list of non-empty strings.

        A provider returning ``None``, a bare string, or a list of non-strings
        must not crash the task (FR-5.3).
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None and str(item).strip()]
        return []

    @property
    def is_empty(self) -> bool:
        return not self.summary.strip() and not self.sub_questions


class HealthReport(BaseModel):
    """Result of a readiness probe (FR-7)."""

    status: str
    checks: dict[str, str] = Field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.status == "ok"
