"""Tool registration, schema export, validation, timeout, and dispatch."""

import asyncio
import inspect
import json
from collections.abc import Iterable
from typing import Any

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from minimal_agent.tools.base import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
    ToolResult,
    ToolRetrySemantics,
)

_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class ToolRegistry:
    """An explicit registry with no decorators or global mutable state."""

    def __init__(self, tools: Iterable[ToolDefinition[Any]] = ()) -> None:
        self._tools: dict[str, ToolDefinition[Any]] = {}
        for tool in tools:
            self.register(tool)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def register(self, tool: ToolDefinition[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        tool.parameters_schema()
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition[Any] | None:
        return self._tools.get(name)

    def openai_tools(self, names: Iterable[str] | None = None) -> list[dict[str, JsonValue]]:
        selected_names = self.names if names is None else tuple(names)
        definitions: list[dict[str, JsonValue]] = []
        for name in selected_names:
            tool = self.get(name)
            if tool is None:
                raise KeyError(f"Tool not registered: {name}")
            definitions.append(tool.to_openai_tool())
        return definitions

    async def execute_json(
        self,
        name: str,
        arguments_json: str,
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as error:
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                "Tool arguments are not valid JSON",
                details={"position": error.pos},
            )
        if not isinstance(arguments, dict):
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                "Tool arguments must be a JSON object",
            )
        return await self.execute(name, arguments, context=context)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(
                "TOOL_NOT_FOUND",
                f"Tool is not registered: {name}",
                details={"tool_name": name},
            )

        validated_arguments, validation_error = self.validate_arguments(tool, arguments)
        if validation_error is not None:
            return validation_error
        if validated_arguments is None:
            raise RuntimeError("Validated tool arguments are missing")

        if tool.retry_semantics is ToolRetrySemantics.WITH_IDEMPOTENCY_KEY and (
            context is None or context.idempotency_key is None
        ):
            return ToolResult.failure(
                "IDEMPOTENCY_CONTEXT_REQUIRED",
                "Tool requires a framework-managed idempotency key",
            )

        try:
            if tool.timeout_seconds is None:
                return await self.invoke_validated(tool, validated_arguments, context=context)
            async with asyncio.timeout(tool.timeout_seconds):
                return await self.invoke_validated(tool, validated_arguments, context=context)
        except TimeoutError:
            message = "Tool timed out before returning a definite result"
            if tool.timeout_seconds is not None:
                message = f"Tool exceeded its {tool.timeout_seconds:g} second timeout"
            return ToolResult.failure(
                "TOOL_TIMEOUT",
                message,
                retryable=True,
                details={"tool_name": name},
            )

    def validate_arguments(
        self,
        tool: ToolDefinition[Any],
        arguments: dict[str, Any],
    ) -> tuple[BaseModel | None, ToolResult | None]:
        try:
            return tool.args_model.model_validate(arguments), None
        except ValidationError as error:
            issues = [
                {
                    "type": issue["type"],
                    "location": [str(part) for part in issue["loc"]],
                    "message": issue["msg"],
                }
                for issue in error.errors(include_url=False, include_input=False)
            ]
            return None, ToolResult.failure(
                "INVALID_ARGUMENTS",
                "Tool arguments failed validation",
                details={"issues": issues},
            )

    async def invoke_validated(
        self,
        tool: ToolDefinition[Any],
        validated_arguments: BaseModel,
        *,
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        try:
            raw_result = await _call_handler(tool, validated_arguments, context)
            result = _JSON_VALUE_ADAPTER.validate_python(raw_result)
        except (TimeoutError, httpx.TimeoutException) as error:
            # A timeout can be raised by the handler itself after its business
            # operation has committed.  Keep that outcome unknown so the
            # durable executor can apply the tool's retry semantics and reuse
            # the original logical call and idempotency key.
            raise TimeoutError("Tool handler timed out") from error
        except ToolExecutionError as error:
            return ToolResult.failure(
                error.code,
                error.message,
                retryable=error.retryable,
                details=error.details,
            )
        except Exception as error:
            return ToolResult.failure(
                "TOOL_FAILED",
                "Tool execution failed unexpectedly",
                details={"tool_name": tool.name, "exception_type": type(error).__name__},
            )

        return ToolResult.success(result)


async def _call_handler(
    tool: ToolDefinition[Any],
    validated_arguments: Any,
    context: ToolExecutionContext | None,
) -> Any:
    parameter_count = len(inspect.signature(tool.handler).parameters)
    arguments = (validated_arguments, context) if parameter_count == 2 else (validated_arguments,)
    if inspect.iscoroutinefunction(tool.handler):
        return await tool.handler(*arguments)
    raw_result = await asyncio.to_thread(tool.handler, *arguments)
    if inspect.isawaitable(raw_result):
        return await raw_result
    return raw_result
