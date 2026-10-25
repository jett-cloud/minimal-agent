"""Tool contracts independent of any LLM SDK."""

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

type ToolHandler[ArgsT: BaseModel] = Callable[..., JsonValue | Awaitable[JsonValue]]

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SCHEMA_METADATA_TO_REMOVE = frozenset({"default", "examples", "title"})


class ToolSideEffect(StrEnum):
    PURE = "pure"
    READ = "read"
    WRITE = "write"


class ToolRetrySemantics(StrEnum):
    SAFE = "safe"
    WITH_IDEMPOTENCY_KEY = "with_idempotency_key"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Framework-owned identity shared by every attempt of one logical tool call."""

    invocation_id: str
    provider_call_id: str
    idempotency_key: str | None
    attempt_no: int


class ToolError(BaseModel):
    """A stable error returned to the agent instead of raising through the loop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value or not value.replace("_", "").isalnum() or value.upper() != value:
            raise ValueError("Tool error codes must use uppercase letters, digits, and underscores")
        return value


class ToolResult(BaseModel):
    """The only result shape exposed by the tool registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    data: JsonValue = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("Successful tool results cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("Failed tool results must contain an error")
        return self

    @classmethod
    def success(cls, data: JsonValue = None) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JsonValue] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            error=ToolError(
                code=code,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
        )


class ToolExecutionError(Exception):
    """An expected handler failure that is safe to report to the agent."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class ToolResultUnknownError(RuntimeError):
    """Raised when every safe attempt ended without a definite result."""

    code = "TOOL_RESULT_UNKNOWN"

    def __init__(self, message: str = "Tool result is unknown after retry") -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolDefinition[ArgsT: BaseModel]:
    """A named, validated callable exposed to the model."""

    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]
    timeout_seconds: float | None = None
    side_effect: ToolSideEffect = ToolSideEffect.PURE
    retry_semantics: ToolRetrySemantics = ToolRetrySemantics.NEVER

    def __post_init__(self) -> None:
        if _TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("Tool name must be 1-64 letters, digits, underscores, or hyphens")
        if not self.description.strip():
            raise ValueError("Tool description must not be empty")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be positive")
        parameter_count = len(inspect.signature(self.handler).parameters)
        if self.retry_semantics is ToolRetrySemantics.WITH_IDEMPOTENCY_KEY:
            if parameter_count != 2:
                raise ValueError(
                    "Tools using with_idempotency_key must accept arguments and execution context"
                )
        elif parameter_count not in {1, 2}:
            raise ValueError("Tool handlers must accept arguments and optional execution context")

    def parameters_schema(self) -> dict[str, JsonValue]:
        """Return a sanitized JSON Schema compatible with strict function calling."""

        raw_schema = self.args_model.model_json_schema(mode="validation")
        schema = _remove_schema_metadata(raw_schema)
        _validate_strict_objects(schema, path=self.name)
        return schema

    def to_openai_tool(self) -> dict[str, JsonValue]:
        """Return the flat function definition used by the Responses API."""

        return {
            "type": "function",
            "name": self.name,
            "description": self.description.strip(),
            "parameters": self.parameters_schema(),
            "strict": True,
        }


def _remove_schema_metadata(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: _remove_schema_metadata(item)
            for key, item in value.items()
            if key not in _SCHEMA_METADATA_TO_REMOVE
        }
    if isinstance(value, list):
        return [_remove_schema_metadata(item) for item in value]
    return value


def _validate_strict_objects(value: JsonValue, *, path: str) -> None:
    if isinstance(value, dict):
        properties = value.get("properties")
        if value.get("type") == "object" or properties is not None:
            if not isinstance(properties, dict):
                raise ValueError(f"Strict schema object at {path} must define properties")
            if value.get("additionalProperties") is not False:
                raise ValueError(f"Strict schema object at {path} must forbid extra properties")
            required = value.get("required")
            if not isinstance(required, list) or set(required) != set(properties):
                raise ValueError(f"Strict schema object at {path} must require every property")
        for key, item in value.items():
            _validate_strict_objects(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_strict_objects(item, path=f"{path}[{index}]")
