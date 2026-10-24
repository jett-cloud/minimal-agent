from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from minimal_agent.config import Settings
from minimal_agent.llm import (
    ContinuationItem,
    FakeLLM,
    LLMError,
    LLMRequest,
    LLMStreamError,
    OpenAIResponsesLLM,
    ReasoningDelta,
    ReasoningDone,
    RequestRetrying,
    ResponseCompleted,
    ResponsesEventMapper,
    ResponseStarted,
    RetryPolicy,
    TextDelta,
    ToolArgumentsDone,
    ToolCallStarted,
    create_openai_sdk_client,
)


def raw_event(event_type: str, sequence_number: int = 1, **values: Any) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, sequence_number=sequence_number, **values)


def response_started(sequence: int = 1) -> SimpleNamespace:
    return raw_event(
        "response.created",
        sequence,
        response=SimpleNamespace(id="resp_1"),
    )


def response_completed(sequence: int = 2) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=12, output_tokens=5, total_tokens=17)
    return raw_event(
        "response.completed",
        sequence,
        response=SimpleNamespace(id="resp_1", usage=usage),
    )


def llm_request() -> LLMRequest:
    return LLMRequest(
        instructions="Be concise.",
        input=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "name": "calculator",
                "description": "Calculate.",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ],
        reasoning_effort="high",
        reasoning_summary="auto",
        max_output_tokens=500,
    )


class FakeResponsesAPI:
    def __init__(self, scripts: list[list[object] | Exception]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[object]:
        self.calls.append(kwargs)
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script

        async def events() -> AsyncIterator[object]:
            for event in script:
                yield event

        return events()


class FakeSDKClient:
    def __init__(self, scripts: list[list[object] | Exception]) -> None:
        self.responses = FakeResponsesAPI(scripts)


def test_event_mapper_maps_text_public_reasoning_and_completion() -> None:
    mapper = ResponsesEventMapper()

    started = mapper.map(response_started())[0]
    reasoning = mapper.map(
        raw_event(
            "response.output_item.added",
            2,
            item=SimpleNamespace(type="reasoning", id="reason_1"),
        )
    )[0]
    reasoning_delta = mapper.map(
        raw_event(
            "response.reasoning_summary_text.delta",
            3,
            item_id="reason_1",
            delta="Checked the inputs.",
        )
    )[0]
    text_delta = mapper.map(
        raw_event("response.output_text.delta", 4, item_id="msg_1", delta="Hello")
    )[0]
    completed = mapper.map(response_completed(5))[0]

    assert isinstance(started, ResponseStarted)
    assert reasoning.type == "reasoning.started"
    assert isinstance(reasoning_delta, ReasoningDelta)
    assert isinstance(text_delta, TextDelta)
    assert isinstance(completed, ResponseCompleted)
    assert completed.total_tokens == 17


def test_event_mapper_exposes_provider_returned_raw_reasoning_text() -> None:
    mapper = ResponsesEventMapper()
    mapper.map(
        raw_event(
            "response.output_item.added",
            item=SimpleNamespace(type="reasoning", id="reason_1"),
        )
    )

    delta = mapper.map(
        raw_event(
            "response.reasoning_text.delta",
            item_id="reason_1",
            delta="private chain of thought",
        )
    )
    done = mapper.map(
        raw_event(
            "response.reasoning_text.done",
            item_id="reason_1",
            text="private chain of thought",
        )
    )[0]

    assert isinstance(delta[0], ReasoningDelta)
    assert delta[0].source == "reasoning_text"
    assert delta[0].content_type == "raw"
    assert isinstance(done, ReasoningDone)
    assert done.text == "private chain of thought"
    assert done.source == "reasoning_text"


def test_event_mapper_assembles_tool_metadata_across_events() -> None:
    mapper = ResponsesEventMapper()
    started = mapper.map(
        raw_event(
            "response.output_item.added",
            1,
            item=SimpleNamespace(
                type="function_call",
                id="item_1",
                call_id="call_1",
                name="calculator",
            ),
        )
    )[0]
    delta = mapper.map(
        raw_event(
            "response.function_call_arguments.delta",
            2,
            item_id="item_1",
            delta='{"expression":',
        )
    )[0]
    done = mapper.map(
        raw_event(
            "response.function_call_arguments.done",
            3,
            item_id="item_1",
            name="calculator",
            arguments='{"expression":"2+2"}',
        )
    )[0]

    assert isinstance(started, ToolCallStarted)
    assert delta.call_id == "call_1"
    assert isinstance(done, ToolArgumentsDone)
    assert done.arguments == '{"expression":"2+2"}'


def test_event_mapper_preserves_reasoning_item_for_tool_continuation() -> None:
    class ReasoningItem:
        type = "reasoning"

        def model_dump_json(self, *, exclude_none: bool) -> str:
            assert exclude_none is True
            return '{"type":"reasoning","id":"reason_1","summary":[]}'

    mapped = ResponsesEventMapper().map(
        raw_event("response.output_item.done", item=ReasoningItem())
    )[0]

    assert isinstance(mapped, ContinuationItem)
    assert mapped.item == {"type": "reasoning", "id": "reason_1", "summary": []}


def test_event_mapper_handles_refusal_terminal_states_and_unknown_events() -> None:
    mapper = ResponsesEventMapper()
    refusal = mapper.map(raw_event("response.refusal.delta", item_id="msg_1", delta="Cannot help"))[
        0
    ]
    refusal_done = mapper.map(
        raw_event("response.refusal.done", item_id="msg_1", refusal="Cannot help")
    )[0]
    incomplete = mapper.map(
        raw_event(
            "response.incomplete",
            response=SimpleNamespace(
                id="resp_2",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            ),
        )
    )[0]
    failed = mapper.map(
        raw_event(
            "response.failed",
            response=SimpleNamespace(
                id="resp_3", error=SimpleNamespace(code="provider_failure", message="private")
            ),
        )
    )[0]

    assert refusal.type == "refusal.delta"
    assert refusal_done.type == "refusal.done"
    assert incomplete.reason == "max_output_tokens"
    assert failed.error_code == "provider_failure"
    assert "private" not in failed.model_dump_json()
    assert mapper.map(raw_event("response.content_part.added")) == ()


@pytest.mark.asyncio
async def test_fake_llm_is_scripted_and_records_requests() -> None:
    request = llm_request()
    fake = FakeLLM([[ResponseStarted(response_id="resp_fake"), TextDelta(item_id="m", delta="x")]])

    events = [event async for event in fake.stream(request)]

    assert [event.type for event in events] == ["response.started", "text.delta"]
    assert fake.requests == [request]
    with pytest.raises(LLMError, match="no scripted response"):
        _ = [event async for event in fake.stream(request)]


@pytest.mark.asyncio
async def test_openai_adapter_builds_responses_request_and_maps_stream() -> None:
    sdk = FakeSDKClient([[response_started(), response_completed()]])
    adapter = OpenAIResponsesLLM(
        sdk,
        model="deepseek-flash",
        retry_policy=RetryPolicy(max_retries=0),
    )

    events = [event async for event in adapter.stream(llm_request())]
    call = sdk.responses.calls[0]

    assert [event.type for event in events] == ["response.started", "response.completed"]
    assert call["model"] == "deepseek-flash"
    assert call["stream"] is True
    assert call["parallel_tool_calls"] is False
    assert call["store"] is False
    assert call["reasoning"] == {"effort": "high", "summary": "auto"}
    assert call["tools"][0]["name"] == "calculator"


@pytest.mark.asyncio
async def test_openai_adapter_retries_timeout_before_output() -> None:
    timeout = APITimeoutError(request=httpx.Request("POST", "https://example.test/responses"))
    sdk = FakeSDKClient([timeout, [response_started(), response_completed()]])
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    adapter = OpenAIResponsesLLM(
        sdk,
        model="deepseek-flash",
        retry_policy=RetryPolicy(max_retries=2, base_delay_seconds=0.1),
        sleep=no_sleep,
    )
    events = [event async for event in adapter.stream(llm_request())]

    assert isinstance(events[0], RequestRetrying)
    assert events[0].error_code == "LLM_TIMEOUT"
    assert events[0].attempt == 2
    assert delays == [0.1]
    assert len(sdk.responses.calls) == 2


def retryable_sdk_errors() -> list[tuple[Callable[[], Exception], str]]:
    request = httpx.Request("POST", "https://example.test/responses")
    return [
        (
            lambda: RateLimitError(
                "private rate error",
                response=httpx.Response(429, request=request),
                body=None,
            ),
            "LLM_RATE_LIMITED",
        ),
        (
            lambda: APIConnectionError(message="private connection error", request=request),
            "LLM_UNAVAILABLE",
        ),
        (
            lambda: InternalServerError(
                "private server error",
                response=httpx.Response(503, request=request),
                body=None,
            ),
            "LLM_UPSTREAM_ERROR",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("error_factory", "expected_code"), retryable_sdk_errors())
async def test_openai_adapter_retries_transient_sdk_errors(
    error_factory: Callable[[], Exception],
    expected_code: str,
) -> None:
    sdk = FakeSDKClient([error_factory(), [response_started(), response_completed()]])

    async def no_sleep(delay: float) -> None:
        assert delay == 0

    adapter = OpenAIResponsesLLM(
        sdk,
        model="deepseek-flash",
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0),
        sleep=no_sleep,
    )
    events = [event async for event in adapter.stream(llm_request())]

    assert isinstance(events[0], RequestRetrying)
    assert events[0].error_code == expected_code
    assert "private" not in events[0].model_dump_json()
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
async def test_openai_adapter_does_not_retry_interrupted_partial_stream() -> None:
    sdk = FakeSDKClient(
        [[response_started(), raw_event("response.output_text.delta", item_id="m", delta="half")]]
    )
    adapter = OpenAIResponsesLLM(
        sdk,
        model="deepseek-flash",
        retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0),
    )

    with pytest.raises(LLMStreamError) as captured:
        _ = [event async for event in adapter.stream(llm_request())]

    assert captured.value.code == "LLM_STREAM_INTERRUPTED"
    assert captured.value.attempts == 1
    assert len(sdk.responses.calls) == 1


@pytest.mark.asyncio
async def test_sdk_client_factory_and_deepseek_setting_aliases(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    settings = Settings(_env_file=None)

    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "secret-test-key"
    assert settings.llm_model == "deepseek-flash"
    assert str(settings.llm_base_url) == "https://api.deepseek.com/"

    client = create_openai_sdk_client(
        settings.llm_api_key,
        base_url=str(settings.llm_base_url),
        timeout_seconds=2,
    )
    try:
        assert str(client.base_url) == "https://api.deepseek.com/"
        assert client.max_retries == 0
        assert client.timeout.connect == 2
    finally:
        await client.close()


def test_generic_llm_settings_are_provider_neutral(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "generic-key")
    monkeypatch.setenv("LLM_MODEL", "generic-model")
    monkeypatch.setenv("LLM_API_STYLE", "chat_completions")
    monkeypatch.setenv("LLM_REASONING_CAPTURE", "both")
    monkeypatch.setenv("LLM_REASONING_OUTPUT", "raw")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "minimal")
    settings = Settings(_env_file=None)

    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "generic-key"
    assert settings.llm_model == "generic-model"
    assert settings.llm_api_style == "chat_completions"
    assert settings.llm_reasoning_capture == "both"
    assert settings.llm_reasoning_output == "raw"
    assert settings.llm_reasoning_effort == "minimal"


def test_blank_optional_reasoning_settings_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    monkeypatch.setenv("LLM_REASONING_SUMMARY", "")

    settings = Settings(_env_file=None)

    assert settings.llm_reasoning_effort is None
    assert settings.llm_reasoning_summary is None
