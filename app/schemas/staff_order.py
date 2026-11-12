from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StaffOrderStatusChange(BaseModel):
    # Reject unexpected fields and trim whitespace from the reason.
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    # Closed orders cannot be the starting point of a new action.
    expected_status: Literal[
        "PLACED",
        "ACCEPTED",
        "PREPARING",
        "READY",
    ]

    status: Literal[
        "ACCEPTED",
        "PREPARING",
        "READY",
        "COMPLETED",
        "REJECTED",
        "CANCELLED",
    ]

    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
    )

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        needs_reason = self.status in {"REJECTED", "CANCELLED"}

        if needs_reason and self.reason is None:
            raise ValueError("A reason is required for rejection or cancellation")

        if not needs_reason and self.reason is not None:
            raise ValueError("Only rejection and cancellation accept a reason")

        return self
