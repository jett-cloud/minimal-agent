"""Validated public HTTP request and response models."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(APIModel):
    title: Annotated[str, Field(max_length=200)] | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class UpdateSessionRequest(APIModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


class CreateMessageRequest(APIModel):
    text: Annotated[str, Field(min_length=1, max_length=1_000_000)]

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class RunAcceptedResponse(APIModel):
    run_id: str
    user_message_id: str
    status: Literal["running"] = "running"
    events_url: str


class APIErrorBody(APIModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class APIErrorResponse(APIModel):
    error: APIErrorBody
