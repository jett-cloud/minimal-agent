"""OpenAI Responses API adapter, also usable with compatible providers."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import SecretStr

from minimal_agent.llm.base import LLMError, LLMRequest, LLMStreamError
from minimal_agent.llm.events import (
    ContinuationItem,
    LLMEvent,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    RefusalDelta,
    RefusalDone,
    RequestRetrying,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    ResponseStarted,
    TextDelta,
    TextDone,
    ToolArgumentsDelta,
    ToolArgumentsDone,
    ToolCallStarted,
    is_terminal_event,
)
from minimal_agent.llm.retry import RetryPolicy

type Sleep = Callable[[float], Awaitable[None]]


class ResponsesEventMapper:
    """Whitelist provider events and normalize them into stable internal events."""

    def __init__(self) -> None:
        self._tool_calls: dict[str, tuple[str, str]] = {}

    def map(self, event: object) -> tuple[LLMEvent, ...]:
        event_type = getattr(event, "type", None)
        sequence = _optional_int(getattr(event, "sequence_number", None))

        if event_type == "response.created":
            response = getattr(event, "response", None)
            return (
                ResponseStarted(
                    response_id=_required_str(response, "id"),
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.in_progress":
            return ()

        if event_type == "response.output_item.added":
            item = getattr(event, "item", None)
            item_type = getattr(item, "type", None)
            item_id = _required_str(item, "id")
            if item_type == "function_call":
                call_id = _required_str(item, "call_id")
                name = _required_str(item, "name")
                self._tool_calls[item_id] = (call_id, name)
                return (
                    ToolCallStarted(
                        item_id=item_id,
                        call_id=call_id,
                        name=name,
                        source_sequence=sequence,
                    ),
                )
            if item_type == "reasoning":
                return (ReasoningStarted(item_id=item_id, source_sequence=sequence),)
            return ()

        if event_type == "response.output_item.done":
            item = getattr(event, "item", None)
            if getattr(item, "type", None) == "reasoning":
                return (
                    ContinuationItem(
                        item=_json_object(item),
                        source_sequence=sequence,
                    ),
                )
            return ()

        if event_type == "response.output_text.delta":
            return (
                TextDelta(
                    item_id=_required_str(event, "item_id"),
                    delta=_required_str(event, "delta", allow_empty=True),
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.output_text.done":
            return (
                TextDone(
                    item_id=_required_str(event, "item_id"),
                    text=_required_str(event, "text", allow_empty=True),
                    source_sequence=sequence,
                ),
            )

        if event_type == "response.reasoning_summary_text.delta":
            return (
                ReasoningDelta(
                    item_id=_required_str(event, "item_id"),
                    delta=_required_str(event, "delta", allow_empty=True),
                    source="reasoning_summary",
                    content_type="summary",
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.reasoning_summary_text.done":
            return (
                ReasoningDone(
                    item_id=_required_str(event, "item_id"),
                    text=_required_str(event, "text", allow_empty=True),
                    source="reasoning_summary",
                    content_type="summary",
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.reasoning_text.delta":
            return (
                ReasoningDelta(
                    item_id=_required_str(event, "item_id"),
                    delta=_required_str(event, "delta", allow_empty=True),
                    source="reasoning_text",
                    content_type="raw",
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.reasoning_text.done":
            return (
                ReasoningDone(
                    item_id=_required_str(event, "item_id"),
                    text=_required_str(event, "text", allow_empty=True),
                    source="reasoning_text",
                    content_type="raw",
                    source_sequence=sequence,
                ),
            )

        if event_type == "response.function_call_arguments.delta":
            item_id = _required_str(event, "item_id")
            call_id, _ = self._get_tool_call(item_id)
            return (
                ToolArgumentsDelta(
                    item_id=item_id,
                    call_id=call_id,
                    delta=_required_str(event, "delta", allow_empty=True),
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.function_call_arguments.done":
            item_id = _required_str(event, "item_id")
            call_id, started_name = self._get_tool_call(item_id)
            done_name = getattr(event, "name", None)
            if not isinstance(done_name, str) or not done_name:
                done_name = started_name
            if done_name != started_name:
                raise LLMStreamError(
                    "INVALID_STREAM_EVENT",
                    "Tool name changed while streaming arguments",
                    retryable=False,
                )
            return (
                ToolArgumentsDone(
                    item_id=item_id,
                    call_id=call_id,
                    name=done_name,
                    arguments=_required_str(event, "arguments", allow_empty=True),
                    source_sequence=sequence,
                ),
            )

        if event_type == "response.refusal.delta":
            return (
                RefusalDelta(
                    item_id=_required_str(event, "item_id"),
                    delta=_required_str(event, "delta", allow_empty=True),
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.refusal.done":
            return (
                RefusalDone(
                    item_id=_required_str(event, "item_id"),
                    refusal=_required_str(event, "refusal", allow_empty=True),
                    source_sequence=sequence,
                ),
            )

        if event_type == "response.completed":
            response = getattr(event, "response", None)
            usage = getattr(response, "usage", None)
            input_tokens = _optional_int(getattr(usage, "input_tokens", None))
            output_tokens = _optional_int(getattr(usage, "output_tokens", None))
            total_tokens = _optional_int(getattr(usage, "total_tokens", None))
            if total_tokens is None and input_tokens is not None and output_tokens is not None:
                total_tokens = input_tokens + output_tokens
            return (
                ResponseCompleted(
                    response_id=_required_str(response, "id"),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.incomplete":
            response = getattr(event, "response", None)
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None)
            return (
                ResponseIncomplete(
                    response_id=_required_str(response, "id"),
                    reason=reason if isinstance(reason, str) else None,
                    source_sequence=sequence,
                ),
            )
        if event_type == "response.failed":
            response = getattr(event, "response", None)
            error = getattr(response, "error", None)
            code = getattr(error, "code", None)
            return (
                ResponseFailed(
                    response_id=_required_str(response, "id"),
                    error_code=code if isinstance(code, str) and code else "LLM_RESPONSE_FAILED",
                    message="The LLM reported a failed response",
                    source_sequence=sequence,
                ),
            )
        return ()

    def _get_tool_call(self, item_id: str) -> tuple[str, str]:
        try:
            return self._tool_calls[item_id]
        except KeyError as error:
            raise LLMStreamError(
                "INVALID_STREAM_EVENT",
                "Tool arguments arrived before the tool call metadata",
                retryable=False,
            ) from error


class OpenAIResponsesLLM:
    """Stream Responses API events with safe retries before semantic output begins."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        retry_policy: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not model.strip():
            raise ValueError("LLM model must not be empty")
        self._client = client
        self._model = model.strip()
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            emitted_semantic_event = False
            saw_terminal = False
            mapper = ResponsesEventMapper()
            try:
                stream = await self._client.responses.create(**self._request_kwargs(request))
                async for raw_event in stream:
                    for event in mapper.map(raw_event):
                        emitted_semantic_event = True
                        saw_terminal = saw_terminal or is_terminal_event(event)
                        yield event
                if not saw_terminal:
                    raise LLMStreamError(
                        "LLM_STREAM_INTERRUPTED",
                        "LLM stream ended without a terminal event",
                        retryable=True,
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                normalized = _normalize_error(error, attempts=attempt)
                can_retry = (
                    normalized.retryable
                    and not emitted_semantic_event
                    and attempt < self._retry_policy.max_attempts
                )
                if not can_retry:
                    raise normalized from error
                delay = self._retry_policy.delay_for_retry(attempt)
                yield RequestRetrying(
                    attempt=attempt + 1,
                    max_attempts=self._retry_policy.max_attempts,
                    error_code=normalized.code,
                    delay_seconds=delay,
                )
                await self._sleep(delay)

    def _request_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "instructions": request.instructions,
            "input": _responses_input(request.input),
            "tools": request.tools,
            "tool_choice": request.tool_choice if request.tools else "none",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
        }
        if request.max_output_tokens is not None:
            kwargs["max_output_tokens"] = request.max_output_tokens
        reasoning: dict[str, str] = {}
        if request.reasoning_effort is not None:
            reasoning["effort"] = request.reasoning_effort
        if request.reasoning_summary is not None:
            reasoning["summary"] = request.reasoning_summary
        if reasoning:
            kwargs["reasoning"] = reasoning
        return kwargs


def _json_object(value: object) -> dict[str, Any]:
    if hasattr(value, "model_dump_json"):
        dumped = json.loads(value.model_dump_json(exclude_none=True))
    elif isinstance(value, dict):
        dumped = value
    else:
        raise LLMStreamError(
            "INVALID_STREAM_EVENT",
            "Reasoning continuation item is not serializable",
            retryable=False,
        )
    if not isinstance(dumped, dict):
        raise LLMStreamError(
            "INVALID_STREAM_EVENT",
            "Reasoning continuation item must be an object",
            retryable=False,
        )
    return dumped


def _responses_input(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") == "function_call" and "reasoning_content" in item:
            normalized.append(
                {key: value for key, value in item.items() if key != "reasoning_content"}
            )
        else:
            normalized.append(item)
    return normalized


def create_openai_sdk_client(
    api_key: SecretStr | str,
    *,
    base_url: str | None = None,
    timeout_seconds: float = 20.0,
) -> AsyncOpenAI:
    """Create an SDK client with SDK retries disabled in favor of adapter retries."""

    secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
    if not secret.strip():
        raise ValueError("LLM API key must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("LLM timeout must be positive")
    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )
    return AsyncOpenAI(
        api_key=secret,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
    )


def _required_str(value: object, attribute: str, *, allow_empty: bool = False) -> str:
    result = getattr(value, attribute, None)
    if not isinstance(result, str) or (not allow_empty and not result):
        raise LLMStreamError(
            "INVALID_STREAM_EVENT",
            f"LLM stream event is missing {attribute}",
            retryable=False,
        )
    return result


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalize_error(error: Exception, *, attempts: int) -> LLMError:
    if isinstance(error, LLMError):
        return type(error)(
            error.code,
            error.message,
            retryable=error.retryable,
            attempts=attempts,
        )
    if isinstance(error, (TimeoutError, APITimeoutError)):
        return LLMError("LLM_TIMEOUT", "LLM request timed out", retryable=True, attempts=attempts)
    if isinstance(error, RateLimitError):
        return LLMError(
            "LLM_RATE_LIMITED", "LLM rate limit was reached", retryable=True, attempts=attempts
        )
    if isinstance(error, APIConnectionError):
        return LLMError(
            "LLM_UNAVAILABLE",
            "LLM provider could not be reached",
            retryable=True,
            attempts=attempts,
        )
    if isinstance(error, APIStatusError):
        return LLMError(
            "LLM_UPSTREAM_ERROR",
            "LLM provider returned an error response",
            retryable=error.status_code >= 500,
            attempts=attempts,
        )
    return LLMError(
        "LLM_FAILED",
        "LLM request failed unexpectedly",
        retryable=False,
        attempts=attempts,
    )
