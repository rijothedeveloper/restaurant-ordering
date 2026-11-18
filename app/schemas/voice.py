import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CallStatus = Literal[
    "received",
    "initiated",
    "ringing",
    "in-progress",
    "completed",
    "busy",
    "no-answer",
    "canceled",
    "failed",
]

TERMINAL_STATUSES = {
    "completed",
    "busy",
    "no-answer",
    "canceled",
    "failed",
}

STATUS_RANK = {
    "received": 0,
    "initiated": 1,
    "ringing": 2,
    "in-progress": 3,
}
STATUS_RANK.update(dict.fromkeys(TERMINAL_STATUSES, 4))

# Provider identifiers are opaque text.
ProviderId = Annotated[
    str,
    Field(min_length=1, max_length=512, pattern=r"^[!-~]+$"),
]

PHONE_PATTERN = r"\+[1-9][0-9]{1,14}"


class TexmlEvent(BaseModel):
    direction: str | None = Field(default=None, alias="Direction", exclude=True)
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    account_sid: ProviderId = Field(alias="AccountSid")
    connection_id: ProviderId = Field(alias="ConnectionId")
    call_sid: ProviderId = Field(alias="CallSid")

    to_phone: str = Field(
        alias="To",
        pattern=rf"^{PHONE_PATTERN}$",
    )
    from_phone: str | None = Field(default=None, alias="From")

    # An instruction-fetch request can omit CallStatus.
    status: CallStatus = Field(default="received", alias="CallStatus")

    sequence: int | None = Field(
        default=None,
        alias="SequenceNumber",
        ge=0,
        le=2**63 - 1,
    )
    callback_source: str | None = Field(
        default=None,
        alias="CallbackSource",
        max_length=64,
    )

    @field_validator("from_phone", mode="before")
    @classmethod
    def normalize_caller(cls, value):
        # Accept withheld numbers and SIP callers without inventing a phone number.
        return (
            value
            if isinstance(value, str) and re.fullmatch(PHONE_PATTERN, value)
            else None
        )
