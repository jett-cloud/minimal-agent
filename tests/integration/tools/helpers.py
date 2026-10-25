from pathlib import Path

import httpx

from minimal_agent.config import Settings
from minimal_agent.llm import (
    FakeLLM,
    LLMEvent,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextDone,
    ToolArgumentsDelta,
    ToolArgumentsDone,
    ToolCallStarted,
)
from minimal_agent.main import create_app
from minimal_agent.tools import ToolRegistry

_ROOT = Path(__file__).resolve().parents[3]


def tool_response(name: str, arguments: str) -> list[LLMEvent]:
    midpoint = max(1, len(arguments) // 2)
    return [
        ResponseStarted(response_id="tool-response"),
        ToolCallStarted(item_id="tool-item", call_id="tool-call", name=name),
        ToolArgumentsDelta(item_id="tool-item", call_id="tool-call", delta=arguments[:midpoint]),
        ToolArgumentsDelta(item_id="tool-item", call_id="tool-call", delta=arguments[midpoint:]),
        ToolArgumentsDone(item_id="tool-item", call_id="tool-call", name=name, arguments=arguments),
        ResponseCompleted(response_id="tool-response"),
    ]


def final_response() -> list[LLMEvent]:
    return [
        ResponseStarted(response_id="final-response"),
        TextDelta(item_id="final-message", delta="done"),
        TextDone(item_id="final-message", text="done"),
        ResponseCompleted(response_id="final-response"),
    ]


async def run_tool_scenario(
    tmp_path: Path,
    *,
    tool_name: str,
    arguments: str,
    registry: ToolRegistry,
) -> tuple[str, list[dict[str, object]], FakeLLM]:
    llm = FakeLLM([tool_response(tool_name, arguments), final_response()])
    settings = Settings(
        database_path=tmp_path / f"{tool_name}.db",
        system_prompt_path=_ROOT / "prompts/system.md",
        summarize_prompt_path=_ROOT / "prompts/summarize.md",
        injected_llm_context_window_tokens=10_000,
        agent_shutdown_grace_seconds=0,
    )
    app = create_app(settings=settings, llm=llm, tools=registry)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        session_id = (await client.post("/v1/sessions", json={})).json()["id"]
        accepted = await client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"text": f"@{tool_name} execute the requested operation"},
        )
        stream = await client.get(accepted.json()["events_url"])
        messages = (await client.get(f"/v1/sessions/{session_id}/messages")).json()
    return stream.text, messages, llm
