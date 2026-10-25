import asyncio

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from minimal_agent.tools import ToolDefinition, ToolExecutionError, ToolRegistry


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def echo(arguments: EchoArguments) -> dict[str, str]:
    return {"text": arguments.text}


def test_registry_exports_responses_api_strict_schema() -> None:
    # 验证工具注册表导出严格的 Responses API Schema。
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="Echo text.",
                args_model=EchoArguments,
                handler=echo,
            )
        ]
    )

    assert registry.names == ("echo",)
    registered = registry.get("echo")
    assert registered is not None
    assert registered.timeout_seconds is None
    assert registry.openai_tools() == [
        {
            "type": "function",
            "name": "echo",
            "description": "Echo text.",
            "strict": True,
            "parameters": {
                "additionalProperties": False,
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "type": "object",
            },
        }
    ]


def test_registry_rejects_duplicates_and_non_strict_models() -> None:
    # 验证工具注册表拒绝重名工具和非严格模型。
    registry = ToolRegistry()
    tool = ToolDefinition(
        name="echo",
        description="Echo text.",
        args_model=EchoArguments,
        handler=echo,
    )
    registry.register(tool)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)

    class OptionalArguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

        optional: str | None = None

    with pytest.raises(ValueError, match="require every property"):
        registry.register(
            ToolDefinition(
                name="optional",
                description="Invalid strict schema.",
                args_model=OptionalArguments,
                handler=lambda arguments: arguments.optional,
            )
        )


@pytest.mark.asyncio
async def test_registry_executes_sync_and_async_handlers() -> None:
    # 验证工具注册表可执行同步和异步处理器。
    async def async_echo(arguments: EchoArguments) -> dict[str, str]:
        await asyncio.sleep(0)
        return {"async": arguments.text}

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="sync_echo",
                description="Sync echo.",
                args_model=EchoArguments,
                handler=echo,
            ),
            ToolDefinition(
                name="async_echo",
                description="Async echo.",
                args_model=EchoArguments,
                handler=async_echo,
            ),
        ]
    )

    sync_result = await registry.execute("sync_echo", {"text": "one"})
    async_result = await registry.execute_json("async_echo", '{"text":"two"}')

    assert sync_result.ok is True
    assert sync_result.data == {"text": "one"}
    assert async_result.ok is True
    assert async_result.data == {"async": "two"}


@pytest.mark.asyncio
async def test_registry_returns_validation_and_lookup_errors() -> None:
    # 验证工具注册表返回参数校验和查找错误。
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="Echo text.",
                args_model=EchoArguments,
                handler=echo,
            )
        ]
    )

    invalid = await registry.execute("echo", {"text": "ok", "extra": True})
    missing = await registry.execute("missing", {})
    malformed = await registry.execute_json("echo", '{"text":')
    non_object = await registry.execute_json("echo", "[]")

    assert invalid.error is not None
    assert invalid.error.code == "INVALID_ARGUMENTS"
    assert invalid.error.details["issues"][0]["type"] == "extra_forbidden"
    assert missing.error is not None
    assert missing.error.code == "TOOL_NOT_FOUND"
    assert malformed.error is not None
    assert malformed.error.code == "INVALID_ARGUMENTS"
    assert non_object.error is not None
    assert non_object.error.code == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_registry_normalizes_timeouts_and_handler_failures() -> None:
    # 验证工具注册表统一超时和处理器故障。
    async def slow(arguments: EchoArguments) -> str:
        await asyncio.sleep(1)
        return arguments.text

    def expected_failure(arguments: EchoArguments) -> str:
        raise ToolExecutionError(
            "UPSTREAM_UNAVAILABLE",
            f"Cannot echo {arguments.text}",
            retryable=True,
        )

    def unexpected_failure(arguments: EchoArguments) -> str:
        raise RuntimeError(arguments.text)

    def handler_timeout(arguments: EchoArguments) -> str:
        raise TimeoutError(arguments.text)

    def handler_http_timeout(arguments: EchoArguments) -> str:
        request = httpx.Request("POST", "https://orders.test")
        raise httpx.ReadTimeout(arguments.text, request=request)

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="slow",
                description="Slow tool.",
                args_model=EchoArguments,
                handler=slow,
                timeout_seconds=0.001,
            ),
            ToolDefinition(
                name="expected_failure",
                description="Expected failure.",
                args_model=EchoArguments,
                handler=expected_failure,
            ),
            ToolDefinition(
                name="unexpected_failure",
                description="Unexpected failure.",
                args_model=EchoArguments,
                handler=unexpected_failure,
            ),
            ToolDefinition(
                name="handler_timeout",
                description="Handler-reported timeout.",
                args_model=EchoArguments,
                handler=handler_timeout,
            ),
            ToolDefinition(
                name="handler_http_timeout",
                description="Handler-reported HTTP timeout.",
                args_model=EchoArguments,
                handler=handler_http_timeout,
            ),
        ]
    )

    timeout = await registry.execute("slow", {"text": "wait"})
    expected = await registry.execute("expected_failure", {"text": "safe"})
    unexpected = await registry.execute("unexpected_failure", {"text": "secret"})
    reported_timeout = await registry.execute("handler_timeout", {"text": "wait"})
    reported_http_timeout = await registry.execute("handler_http_timeout", {"text": "wait"})

    assert timeout.error is not None
    assert timeout.error.code == "TOOL_TIMEOUT"
    assert timeout.error.retryable is True
    assert expected.error is not None
    assert expected.error.code == "UPSTREAM_UNAVAILABLE"
    assert expected.error.retryable is True
    assert unexpected.error is not None
    assert unexpected.error.code == "TOOL_FAILED"
    assert unexpected.error.message == "Tool execution failed unexpectedly"
    assert "secret" not in unexpected.model_dump_json()
    assert reported_timeout.error is not None
    assert reported_timeout.error.code == "TOOL_TIMEOUT"
    assert reported_timeout.error.retryable is True
    assert reported_http_timeout.error is not None
    assert reported_http_timeout.error.code == "TOOL_TIMEOUT"
    assert reported_http_timeout.error.retryable is True
