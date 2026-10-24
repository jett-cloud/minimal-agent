"""LLM request and client contracts independent of any provider SDK."""

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from minimal_agent.llm.events import LLMEvent


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: str
    input: list[dict[str, JsonValue]]
    tools: list[dict[str, JsonValue]] = Field(default_factory=list)
    tool_choice: JsonValue = "auto"
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = (
        None
    )
    reasoning_summary: Literal["auto", "concise", "detailed"] | None = None


class LLMClient(Protocol):
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]: ...


class LLMError(RuntimeError):
    """A stable LLM failure safe for AgentLoop and Trace handling."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


class LLMStreamError(LLMError):
    """Raised when a stream cannot safely produce a terminal event."""
