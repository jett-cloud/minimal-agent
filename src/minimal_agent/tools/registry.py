"""Tool registration, schema export, validation, timeout, and dispatch."""

import asyncio
import inspect
import json
from collections.abc import Iterable
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from minimal_agent.tools.base import ToolDefinition, ToolExecutionError, ToolResult

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

    async def execute_json(self, name: str, arguments_json: str) -> ToolResult:
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
        return await self.execute(name, arguments)

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(
                "TOOL_NOT_FOUND",
                f"Tool is not registered: {name}",
                details={"tool_name": name},
            )

        try:
            validated_arguments = tool.args_model.model_validate(arguments)
        except ValidationError as error:
            issues = [
                {
                    "type": issue["type"],
                    "location": [str(part) for part in issue["loc"]],
                    "message": issue["msg"],
                }
                for issue in error.errors(include_url=False, include_input=False)
            ]
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                "Tool arguments failed validation",
                details={"issues": issues},
            )

        try:
            async with asyncio.timeout(tool.timeout_seconds):
                if inspect.iscoroutinefunction(tool.handler):
                    raw_result = await tool.handler(validated_arguments)
                else:
                    raw_result = await asyncio.to_thread(tool.handler, validated_arguments)
                    if inspect.isawaitable(raw_result):
                        raw_result = await raw_result
                result = _JSON_VALUE_ADAPTER.validate_python(raw_result)
        except TimeoutError:
            return ToolResult.failure(
                "TOOL_TIMEOUT",
                f"Tool exceeded its {tool.timeout_seconds:g} second timeout",
                retryable=True,
                details={"tool_name": name},
            )
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
                details={"tool_name": name, "exception_type": type(error).__name__},
            )

        return ToolResult.success(result)
