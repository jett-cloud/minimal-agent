import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from minimal_agent.tools import ToolDefinition, ToolExecutionError, ToolRegistry


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def echo(arguments: EchoArguments) -> dict[str, str]:
    return {"text": arguments.text}


def test_registry_exports_responses_api_strict_schema() -> None:
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
        ]
    )

    timeout = await registry.execute("slow", {"text": "wait"})
    expected = await registry.execute("expected_failure", {"text": "safe"})
    unexpected = await registry.execute("unexpected_failure", {"text": "secret"})

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
