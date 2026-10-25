from pathlib import Path

import pytest

from minimal_agent.tools import CALCULATOR_TOOL, ToolRegistry
from tests.integration.tools.helpers import run_tool_scenario


@pytest.mark.asyncio
async def test_agent_calculator_success_is_persisted_and_returned_to_model(tmp_path: Path) -> None:
    # 验证 Agent 的计算结果会持久化并返回模型。
    stream, messages, llm = await run_tool_scenario(
        tmp_path,
        tool_name="calculator",
        arguments='{"expression":"(12+8)*3"}',
        registry=ToolRegistry([CALCULATOR_TOOL]),
    )

    assert "event: tool.completed" in stream
    assert messages[2]["content"]["result"]["data"]["result"] == "60"
    assert "60" in str(llm.requests[1].input)
