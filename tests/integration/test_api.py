import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from minimal_agent.config import Settings
from minimal_agent.llm import (
    FakeLLM,
    LLMEvent,
    LLMRequest,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextDone,
    ToolArgumentsDelta,
    ToolArgumentsDone,
    ToolCallStarted,
)
from minimal_agent.main import create_app
from minimal_agent.schemas import RunStatus
from minimal_agent.services import build_services
from minimal_agent.tools import CALCULATOR_TOOL, ToolRegistry, create_mainboard_search_tool

_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """构造测试用 Settings：使用临时库与极小的心跳/优雅关闭时长，便于快速跑通集成测试。"""
    values: dict[str, object] = {
        "database_path": tmp_path / "api.db",
        "system_prompt_path": _ROOT / "prompts/system.md",
        "summarize_prompt_path": _ROOT / "prompts/summarize.md",
        "context_window_tokens": 10_000,
        "sse_heartbeat_seconds": 0.01,
        "agent_shutdown_grace_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _final_response(text: str, suffix: str) -> list[LLMEvent]:
    """构造一段只产出纯文本的 LLM 流式事件序列（无工具调用）。"""
    return [
        ResponseStarted(response_id=f"response_{suffix}"),
        TextDelta(item_id=f"message_{suffix}", delta=text),
        TextDone(item_id=f"message_{suffix}", text=text),
        ResponseCompleted(response_id=f"response_{suffix}"),
    ]


def _tool_response(name: str, arguments: str, suffix: str) -> list[LLMEvent]:
    """构造一段工具调用的 LLM 流式事件序列（参数分两段 delta + 一次 done）。"""
    call_id = f"call_{suffix}"
    item_id = f"tool_{suffix}"
    midpoint = max(1, len(arguments) // 2)
    return [
        ResponseStarted(response_id=f"response_{suffix}"),
        ToolCallStarted(item_id=item_id, call_id=call_id, name=name),
        ToolArgumentsDelta(
            item_id=item_id,
            call_id=call_id,
            delta=arguments[:midpoint],
        ),
        ToolArgumentsDelta(
            item_id=item_id,
            call_id=call_id,
            delta=arguments[midpoint:],
        ),
        ToolArgumentsDone(
            item_id=item_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
        ),
        ResponseCompleted(response_id=f"response_{suffix}"),
    ]


@pytest.mark.asyncio
async def test_full_api_happy_path_and_durable_sse(tmp_path: Path) -> None:
    """端到端 happy path：创建会话、发送消息，验证持久化 SSE 事件流、运行状态、trace 与消息落库。"""
    llm = FakeLLM([_final_response("answer", "1")])
    app = create_app(
        settings=_settings(tmp_path),
        llm=llm,
        tools=ToolRegistry(),
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        created = await client.post("/v1/sessions", json={"title": " Demo "})
        session_id = created.json()["id"]
        listed = await client.get("/v1/sessions")
        fetched = await client.get(f"/v1/sessions/{session_id}")
        accepted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": " hello "},
        )
        accepted_body = accepted.json()
        stream = await client.get(accepted_body["events_url"])
        run = await client.get(f"/v1/runs/{accepted_body['run_id']}")
        trace = await client.get(f"/v1/runs/{accepted_body['run_id']}/trace")
        messages = await client.get(f"/v1/sessions/{session_id}/messages")

    assert created.status_code == 201
    assert created.json()["title"] == "Demo"
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [session_id]
    assert fetched.json()["id"] == session_id
    assert accepted.status_code == 202
    assert accepted_body["status"] == "running"
    assert accepted_body["events_url"].endswith(f"/{accepted_body['run_id']}/events")
    assert stream.status_code == 200
    assert "event: assistant.delta" in stream.text
    assert "event: run.completed" in stream.text
    assert run.json()["status"] == "completed"
    assert trace.json()[-1]["event_type"] == "run.completed"
    assert [message["content"]["text"] for message in messages.json()] == ["hello", "answer"]


@pytest.mark.asyncio
async def test_full_api_executes_multiple_tools_before_final_answer(tmp_path: Path) -> None:
    """模型连续调用 calculator 与 search 两个工具后再给出最终回答，验证多工具编排与消息序列。"""
    llm = FakeLLM(
        [
            _tool_response("calculator", '{"expression":"(12+8)*3"}', "calc"),
            _tool_response(
                "search",
                '{"query":"B650 Wi-Fi 6E Mini-ITX","brand":null,"limit":2}',
                "search",
            ),
            _final_response("计算结果为 60，并找到了主板资料。", "final"),
        ]
    )
    tools = ToolRegistry(
        [
            CALCULATOR_TOOL,
            create_mainboard_search_tool(_ROOT / "data/mainboards.json"),
        ]
    )
    app = create_app(settings=_settings(tmp_path), llm=llm, tools=tools)
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        session_id = (await client.post("/v1/sessions", json={})).json()["id"]
        accepted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": "帮我计算并查询一款 B650 Mini-ITX 主板"},
        )
        stream = await client.get(accepted.json()["events_url"])
        messages = (await client.get(f"/v1/sessions/{session_id}/messages")).json()

    assert stream.text.count("event: tool.completed") == 2
    assert "event: run.completed" in stream.text
    assert [message["kind"] for message in messages] == [
        "message",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "message",
    ]
    assert messages[2]["content"]["result"]["data"]["result"] == "60"
    assert messages[4]["content"]["result"]["data"]["count"] > 0
    assert len(llm.requests) == 3


@pytest.mark.asyncio
async def test_full_api_preserves_session_memory_without_cross_session_leakage(
    tmp_path: Path,
) -> None:
    """同会话后续提问能看到本会话历史，且不会泄漏其他会话的内容，验证会话上下文隔离。"""
    llm = FakeLLM(
        [
            _final_response("alpha-answer", "a1"),
            _final_response("beta-answer", "b1"),
            _final_response("alpha-follow-up", "a2"),
        ]
    )
    app = create_app(settings=_settings(tmp_path), llm=llm, tools=ToolRegistry())
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        alpha = (await client.post("/v1/sessions", json={})).json()["id"]
        beta = (await client.post("/v1/sessions", json={})).json()["id"]
        for session_id, text in ((alpha, "alpha-fact"), (beta, "beta-secret")):
            accepted = await client.post(
                f"/v1/sessions/{session_id}/messages",
                json={"text": text},
            )
            await client.get(accepted.json()["events_url"])
        follow_up = await client.post(
            f"/v1/sessions/{alpha}/messages",
            json={"text": "what did I say?"},
        )
        await client.get(follow_up.json()["events_url"])

    alpha_context = str(llm.requests[2].input)
    assert "alpha-fact" in alpha_context
    assert "alpha-answer" in alpha_context
    assert "beta-secret" not in alpha_context
    assert "beta-answer" not in alpha_context


@pytest.mark.asyncio
async def test_unexpected_llm_failure_is_durable_and_does_not_leak_details(
    tmp_path: Path,
) -> None:
    """LLM 抛出未预期异常时，运行应被持久化为 failed 且异常细节（含密钥）不得泄漏到响应中。"""
    secret = "sk-should-never-escape"
    app = create_app(
        settings=_settings(tmp_path),
        llm=FakeLLM([RuntimeError(secret)]),
        tools=ToolRegistry(),
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        session_id = (await client.post("/v1/sessions", json={})).json()["id"]
        accepted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": "trigger failure"},
        )
        run_id = accepted.json()["run_id"]
        stream = await client.get(accepted.json()["events_url"])
        run = await client.get(f"/v1/runs/{run_id}")
        trace = await client.get(f"/v1/runs/{run_id}/trace")

    serialized = stream.text + run.text + trace.text
    assert "event: run.failed" in stream.text
    assert run.json()["error_code"] == "INTERNAL_ERROR"
    assert secret not in serialized


@pytest.mark.asyncio
async def test_api_returns_stable_404_422_and_503_errors(tmp_path: Path) -> None:
    """验证 404、422、503 错误契约，并确保不泄漏密钥。"""
    unavailable_app = create_app(settings=_settings(tmp_path, llm_api_key=None))
    transport = httpx.ASGITransport(app=unavailable_app)

    async with (
        unavailable_app.router.lifespan_context(unavailable_app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        missing = await client.get("/v1/sessions/missing")
        invalid = await client.post("/v1/sessions", json={"unexpected": True})
        created = await client.post("/v1/sessions", json={})
        unavailable = await client.post(
            f"/v1/sessions/{created.json()['id']}/messages",
            json={"text": "hello"},
        )
        missing_run = await client.get("/v1/runs/missing")
        missing_events = await client.get("/v1/runs/missing/events")
        blank_message = await client.post(
            f"/v1/sessions/{created.json()['id']}/messages",
            json={"text": "   "},
        )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "AGENT_UNAVAILABLE"
    assert "secret" not in unavailable.text.casefold()
    assert missing_run.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert missing_events.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert blank_message.status_code == 422
    assert blank_message.json()["error"]["code"] == "VALIDATION_ERROR"


class _BlockedLLM:
    """测试用 LLM：在第二个请求到达后阻塞所有请求，用于构造并发冲突场景。"""

    def __init__(self) -> None:
        self.started = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, _request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.started += 1
        if self.started >= 2:
            self.all_started.set()
        await self.release.wait()
        for event in _final_response("done", str(self.started)):
            yield event


@pytest.mark.asyncio
async def test_same_session_conflicts_while_different_sessions_run_concurrently(
    tmp_path: Path,
) -> None:
    """同一会话并发消息返回 409 冲突，而不同会话可并行运行，验证会话级串行 + 跨会话并发。"""
    llm = _BlockedLLM()
    app = create_app(
        settings=_settings(tmp_path),
        llm=llm,
        tools=ToolRegistry(),
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        first_session = (await client.post("/v1/sessions", json={})).json()["id"]
        second_session = (await client.post("/v1/sessions", json={})).json()["id"]
        first = await client.post(f"/v1/sessions/{first_session}/messages", json={"text": "one"})
        conflict = await client.post(f"/v1/sessions/{first_session}/messages", json={"text": "two"})
        second = await client.post(
            f"/v1/sessions/{second_session}/messages", json={"text": "three"}
        )
        await asyncio.wait_for(llm.all_started.wait(), timeout=1)
        assert len(app.state.services.supervisor.active_run_ids) == 2
        llm.release.set()
        await app.state.services.supervisor.shutdown(grace_seconds=1)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "SESSION_BUSY"
    assert second.status_code == 202


@pytest.mark.asyncio
async def test_lifespan_recovers_interrupted_runs(tmp_path: Path) -> None:
    """启动时把遗留 running 运行标记为 PROCESS_RESTARTED 并补写事件。"""
    settings = _settings(tmp_path)
    services = build_services(
        settings,
        llm=FakeLLM([]),
        tools=ToolRegistry(),
    )
    session = services.sessions.create()
    _, run = services.runs.create_with_user_message(session.id, text="interrupted")
    app = create_app(settings=settings, services=services)

    async with app.router.lifespan_context(app):
        recovered = services.runs.require(run.id)

    assert recovered.status is RunStatus.FAILED
    assert recovered.error_code == "PROCESS_RESTARTED"
    assert services.stream_events.list_after_id(run.id)[-1].event_type == "run.failed"


def test_openapi_exposes_complete_api_and_error_contract(tmp_path: Path) -> None:
    """校验 OpenAPI 文档暴露了全部路径，并包含消息接口的 202/409 响应及统一错误模型。"""
    app = create_app(
        settings=_settings(tmp_path),
        llm=FakeLLM([]),
        tools=ToolRegistry(),
    )

    schema = app.openapi()

    assert set(schema["paths"]) == {
        "/health",
        "/v1/sessions",
        "/v1/sessions/{session_id}",
        "/v1/sessions/{session_id}/messages",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/trace",
        "/v1/runs/{run_id}/events",
    }
    message_responses = schema["paths"]["/v1/sessions/{session_id}/messages"]["post"]["responses"]
    assert "202" in message_responses
    assert "409" in message_responses
    assert "APIErrorResponse" in schema["components"]["schemas"]
