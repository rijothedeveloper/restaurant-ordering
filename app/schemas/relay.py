from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.schemas.voice import ProviderId


class Frame(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        hide_input_in_errors=True,
    )


class Parameters(Frame):
    relay_token: str = Field(
        pattern=r"^[A-Za-z0-9_-]{43}$",
        min_length=43,
        max_length=43,
        repr=False,
    )


class Setup(Frame):
    type: Literal["setup"]
    session_id: ProviderId = Field(alias="sessionId")
    account_sid: ProviderId = Field(alias="accountSid")
    call_sid: ProviderId = Field(alias="callSid")
    call_control_id: ProviderId = Field(alias="callControlId")
    to_phone: str | None = Field(alias="to", max_length=32)
    direction: Literal["inbound"] | None
    parameters: Parameters = Field(alias="customParameters")


class Prompt(Frame):
    type: Literal["prompt"]
    voice_prompt: str = Field(alias="voicePrompt", max_length=4000)
    last: bool


class Dtmf(Frame):
    type: Literal["dtmf"]
    digit: str = Field(
        pattern=r"^[0-9A-D*#]$",
        min_length=1,
        max_length=1,
    )

class RelayInfo(Frame):
    type: Literal["info"]
    name: str
    value: str

class Interrupt(Frame):
    type: Literal["interrupt"]
    utterance: str = Field(alias="utteranceUntilInterrupt", max_length=4000)
    duration_ms: int = Field(alias="durationUntilInterruptMs", ge=0)


class RelayError(Frame):
    type: Literal["error"]
    description: str = Field(max_length=4000)


# Select the appropriate model using the incoming "type" field.
INCOMING = TypeAdapter(
    Annotated[
        Prompt | Dtmf | Interrupt | RelayError | RelayInfo,
        Field(discriminator="type"),
    ]
)
