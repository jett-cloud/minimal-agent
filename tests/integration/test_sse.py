import httpx
import pytest
from fastapi import FastAPI

from minimal_agent.api.errors import install_error_handlers
from minimal_agent.api.events import router
from minimal_agent.config import Settings, get_settings
from minimal_agent.dependencies import (
    get_event_publisher,
    get_run_repository,
    get_stream_event_repository,
)
from minimal_agent.schemas import MessageKind, MessageRole
from minimal_agent.storage import (
    Database,
    MessageRepository,
    RunRepository,
    SessionRepository,
    StreamEventRepository,
)
from minimal_agent.streaming import (
    AssistantDeltaEvent,
    EventPublisher,
    RunCompletedEvent,
    RunStartedEvent,
)


def _test_app(database: Database) -> tuple[FastAPI, str, list[int]]:
    """创建带 3 条事件的 run，并返回 app、run_id 和事件 id。"""
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    events = StreamEventRepository(database)
    session = sessions.create()
    user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "hello"},
    )
    run = runs.create(session.id, user_message_id=user.id)
    records = events.append_batch(
        run.id,
        [
            RunStartedEvent(run_id=run.id, session_id=session.id).to_pending(),
            AssistantDeltaEvent(item_id="item_1", delta="hello").to_pending(),
            RunCompletedEvent(run_id=run.id, message_id="message_1").to_pending(),
        ],
    )

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_stream_event_repository] = lambda: events
    app.dependency_overrides[get_run_repository] = lambda: runs
    app.dependency_overrides[get_event_publisher] = EventPublisher
    app.dependency_overrides[get_settings] = lambda: Settings(sse_heartbeat_seconds=0.01)
    return app, run.id, [record.id for record in records]


@pytest.mark.asyncio
async def test_sse_endpoint_replays_from_last_event_id(database: Database) -> None:
    # 验证 SSE 可从最后事件编号继续重放。
    """携带 Last-Event-ID 头时，从该事件之后开始回放，并校验 SSE 响应头格式。"""
    app, run_id, ids = _test_app(database)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(ids[0])},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert f"id: {ids[0]}\n" not in response.text
    assert f"id: {ids[1]}\n" in response.text
    assert f"id: {ids[2]}\n" in response.text
    assert response.text.count("event: assistant.delta") == 1


@pytest.mark.asyncio
async def test_after_id_takes_precedence_and_errors_are_bounded(database: Database) -> None:
    # 验证 after_id 优先级及错误响应大小边界。
    """query 参数 after_id 优先于 Last-Event-ID 头；非法 ID 返回 400，缺失 run 返回 404。"""
    app, run_id, ids = _test_app(database)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resumed = await client.get(
            f"/v1/runs/{run_id}/events",
            params={"after_id": ids[1]},
            headers={"Last-Event-ID": "999999"},
        )
        invalid = await client.get(
            f"/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        missing = await client.get("/v1/runs/missing/events")

    assert resumed.status_code == 200
    assert f"id: {ids[2]}\n" in resumed.text
    assert f"id: {ids[1]}\n" not in resumed.text
    assert invalid.status_code == 400
    assert missing.status_code == 404
