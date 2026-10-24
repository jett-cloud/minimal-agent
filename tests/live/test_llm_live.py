"""Opt-in, provider-neutral live acceptance tests with a hard LLM call budget."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from minimal_agent.config import Settings
from minimal_agent.llm import (
    LLMClient,
    LLMEvent,
    LLMRequest,
    OpenAIChatCompletionsLLM,
    OpenAIResponsesLLM,
    ReasoningAwareLLM,
    ReasoningDelta,
    ReasoningStarted,
    ResponseCompleted,
    RetryPolicy,
    TextDelta,
    create_openai_sdk_client,
)
from minimal_agent.main import create_app
from minimal_agent.tools import (
    CALCULATOR_TOOL,
    ToolRegistry,
    create_mainboard_search_tool,
    create_weather_tool,
)

_ROOT = Path(__file__).resolve().parents[2]


class _CallBudgetLLM:
    def __init__(self, inner: LLMClient, *, maximum: int) -> None:
        self.inner = inner
        self.maximum = maximum
        self.calls = 0

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        if self.calls > self.maximum:
            raise AssertionError(f"live LLM call budget exceeded: {self.calls}>{self.maximum}")
        async for event in self.inner.stream(request):
            yield event


def _require_live_profile(profile: str) -> Settings:
    if os.getenv("RUN_LIVE_LLM_TESTS") != "1":
        pytest.skip("set RUN_LIVE_LLM_TESTS=1 to enable paid live tests")
    selected = os.getenv("LIVE_TEST_PROFILE", "smoke")
    if selected != profile:
        pytest.skip(f"set LIVE_TEST_PROFILE={profile} to run this profile")
    settings = Settings()
    if settings.llm_api_key is None or settings.llm_model is None:
        pytest.fail("LLM_API_KEY and LLM_MODEL are required for live tests")
    return settings


def _live_client(settings: Settings, *, budget: int) -> tuple[Any, _CallBudgetLLM]:
    assert settings.llm_api_key is not None
    assert settings.llm_model is not None
    sdk = create_openai_sdk_client(
        settings.llm_api_key,
        base_url=str(settings.llm_base_url) if settings.llm_base_url is not None else None,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    adapter_type = (
        OpenAIResponsesLLM if settings.llm_api_style == "responses" else OpenAIChatCompletionsLLM
    )
    adapter = adapter_type(
        sdk,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        retry_policy=RetryPolicy(max_retries=settings.llm_max_retries),
    )
    reasoning = ReasoningAwareLLM(
        adapter,
        capture=settings.llm_reasoning_capture,
        output=settings.llm_reasoning_output,
        think_tag_enabled=settings.llm_think_tag_enabled,
        think_tag_name=settings.llm_think_tag_name,
    )
    return sdk, _CallBudgetLLM(reasoning, maximum=budget)


@pytest.mark.live
@pytest.mark.asyncio
async def test_configured_model_smoke_live() -> None:
    settings = _require_live_profile("smoke")
    budget = int(os.getenv("LIVE_MAX_LLM_CALLS", "1"))
    output_limit = int(os.getenv("LIVE_MAX_OUTPUT_TOKENS", "64"))
    sdk, llm = _live_client(settings, budget=budget)
    try:
        events = [
            event
            async for event in llm.stream(
                LLMRequest(
                    instructions="Reply with exactly the word pong.",
                    input=[{"role": "user", "content": "ping"}],
                    max_output_tokens=min(settings.llm_max_output_tokens, output_limit),
                    reasoning_effort=settings.llm_reasoning_effort,
                    reasoning_summary=settings.llm_reasoning_summary,
                )
            )
        ]
    finally:
        await sdk.close()

    assert any(isinstance(event, TextDelta) and event.delta for event in events)
    assert any(isinstance(event, ResponseCompleted) for event in events)
    _assert_reasoning_events(events)
    assert llm.calls <= budget


@pytest.mark.live
@pytest.mark.asyncio
async def test_configured_model_tools_compression_reasoning_full_live(tmp_path: Path) -> None:
    base = _require_live_profile("full")
    budget = int(os.getenv("LIVE_MAX_LLM_CALLS", "8"))
    output_limit = int(os.getenv("LIVE_MAX_OUTPUT_TOKENS", "1024"))
    settings = base.model_copy(
        update={
            "database_path": tmp_path / "live.db",
            "context_window_tokens": 2_500,
            "llm_max_output_tokens": min(base.llm_max_output_tokens, output_limit),
            "agent_shutdown_grace_seconds": 0,
        }
    )
    sdk, llm = _live_client(settings, budget=budget)
    expect_tools = os.getenv("LIVE_EXPECT_TOOLS", "true").casefold() == "true"

    def weather_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=_geocoding_payload())
        return httpx.Response(200, json=_forecast_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(weather_handler)) as weather:
        tools = ToolRegistry(
            [
                CALCULATOR_TOOL,
                create_mainboard_search_tool(settings.mainboard_data_path),
                create_weather_tool(weather),
            ]
        )
        app = create_app(settings=settings, llm=llm, tools=tools)
        transport = httpx.ASGITransport(app=app)
        streams: list[str] = []
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            ):
                cases = [
                    ("calculator", "@calculator 计算 (125+75)*3，必须调用工具后回答。"),
                    (
                        "search",
                        "@search 查询 ASUS ROG STRIX B650E-I 主板资料，必须调用工具。",
                    ),
                    ("weather", "@weather 查询 Beijing 当前天气，必须调用工具。"),
                ]
                for tool_name, prompt in cases:
                    stream = await _send_message(client, prompt)
                    if expect_tools:
                        assert "event: tool.completed" in stream
                        assert f'"name":"{tool_name}"' in stream
                    streams.append(stream)

                compression_session = app.state.services.sessions.create(title="compression")
                for index in range(7):
                    padding = ("old-history-" * 500) if index < 2 else f"short-{index}"
                    _, run = app.state.services.runs.create_with_user_message(
                        compression_session.id,
                        text=f"turn-{index}: {padding}",
                    )
                    app.state.services.runs.complete_with_final_answer(
                        run.id,
                        text=f"completed-{index}",
                    )
                compression_stream = await _send_message(
                    client,
                    "Do not call a tool. Reply briefly that prior context was summarized.",
                    session_id=compression_session.id,
                )
                streams.append(compression_stream)
                compressed = app.state.services.sessions.require(compression_session.id)
                assert compressed.summary
                assert compressed.summary_through_seq is not None
        finally:
            await sdk.close()

    assert llm.calls <= budget
    assert not expect_tools or all("event: tool.completed" in stream for stream in streams[:3])
    _assert_reasoning_sse("".join(streams))


async def _send_message(
    client: httpx.AsyncClient,
    prompt: str,
    *,
    session_id: str | None = None,
) -> str:
    if session_id is None:
        session_id = (await client.post("/v1/sessions", json={})).json()["id"]
    accepted = await client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"text": prompt},
    )
    assert accepted.status_code == 202, accepted.text
    stream = await client.get(accepted.json()["events_url"])
    assert stream.status_code == 200
    assert "event: run.completed" in stream.text, stream.text
    return stream.text


def _assert_reasoning_events(events: list[LLMEvent]) -> None:
    expectation = os.getenv("LIVE_EXPECT_REASONING", "none")
    deltas = [event for event in events if isinstance(event, ReasoningDelta)]
    if expectation == "none":
        return
    if expectation == "activity":
        assert any(isinstance(event, ReasoningStarted) for event in events)
        return
    assert any(event.content_type == expectation for event in deltas)
    source = os.getenv("LIVE_EXPECT_REASONING_SOURCE", "either")
    if source == "tagged":
        assert any(event.source == "think_tag" for event in deltas)
    elif source == "structured":
        assert any(event.source != "think_tag" for event in deltas)


def _assert_reasoning_sse(stream: str) -> None:
    expectation = os.getenv("LIVE_EXPECT_REASONING", "none")
    events = _parse_sse(stream)
    reasoning = [item for event_type, item in events if event_type.startswith("reasoning.")]
    if expectation == "none":
        return
    assert reasoning
    if expectation != "activity":
        assert any(
            item.get("content_type") == expectation and item.get("delta") for item in reasoning
        )
    source = os.getenv("LIVE_EXPECT_REASONING_SOURCE", "either")
    if source == "tagged":
        assert any(item.get("source") == "think_tag" for item in reasoning)
    elif source == "structured":
        assert any(item.get("source") not in {"think_tag", "lifecycle"} for item in reasoning)


def _parse_sse(stream: str) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in stream.replace("\r\n", "\n").split("\n\n"):
        event_type = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if event_type and data:
            parsed.append((event_type, json.loads(data)))
    return parsed


def _geocoding_payload() -> dict[str, Any]:
    return {
        "results": [
            {
                "id": 1816670,
                "name": "Beijing",
                "latitude": 39.9075,
                "longitude": 116.39723,
                "timezone": "Asia/Shanghai",
                "country_code": "CN",
                "country": "China",
                "admin1": "Beijing",
                "population": 11716620,
            }
        ]
    }


def _forecast_payload() -> dict[str, Any]:
    return {
        "latitude": 39.9,
        "longitude": 116.4,
        "generationtime_ms": 0.2,
        "utc_offset_seconds": 28800,
        "timezone": "Asia/Shanghai",
        "timezone_abbreviation": "GMT+8",
        "elevation": 51.0,
        "current_units": {
            "time": "iso8601",
            "interval": "seconds",
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "apparent_temperature": "°C",
            "precipitation": "mm",
            "weather_code": "wmo code",
            "cloud_cover": "%",
            "wind_speed_10m": "km/h",
            "wind_direction_10m": "°",
            "is_day": "",
        },
        "current": {
            "time": "2026-10-24T14:15",
            "interval": 900,
            "temperature_2m": 18.4,
            "relative_humidity_2m": 42,
            "apparent_temperature": 17.1,
            "precipitation": 0.0,
            "weather_code": 2,
            "cloud_cover": 45,
            "wind_speed_10m": 12.3,
            "wind_direction_10m": 315,
            "is_day": 1,
        },
        "daily_units": {
            "time": "iso8601",
            "weather_code": "wmo code",
            "temperature_2m_max": "°C",
            "temperature_2m_min": "°C",
            "precipitation_probability_max": "%",
            "precipitation_sum": "mm",
            "wind_speed_10m_max": "km/h",
        },
        "daily": {
            "time": ["2026-10-24"],
            "weather_code": [2],
            "temperature_2m_max": [20.2],
            "temperature_2m_min": [9.1],
            "precipitation_probability_max": [10],
            "precipitation_sum": [0.0],
            "wind_speed_10m_max": [18.0],
        },
    }
