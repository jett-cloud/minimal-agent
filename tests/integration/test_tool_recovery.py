import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from minimal_agent.config import Settings
from minimal_agent.llm import FakeLLM
from minimal_agent.schemas import MessageKind, MessageRole, OrderStatus, RunStatus
from minimal_agent.services import build_services
from minimal_agent.storage import (
    Database,
    MessageRepository,
    OrderRepository,
    RunRepository,
    SessionRepository,
    ToolInvocationRepository,
)
from minimal_agent.tools import DurableToolExecutor, ToolRegistry, create_order_tools
from tests.integration.tools.helpers import final_response

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_rebuilt_services_resume_same_order_call_without_duplicate(tmp_path: Path) -> None:
    # 模拟业务已提交但进程尚未记录工具结果时重启，验证恢复仍复用同一幂等键。
    database_path = tmp_path / "restart-recovery.db"
    original_database = Database(database_path)
    original_database.initialize()
    sessions = SessionRepository(original_database)
    messages = MessageRepository(original_database)
    runs = RunRepository(original_database)
    orders = OrderRepository(original_database)
    invocations = ToolInvocationRepository(original_database)

    session = sessions.create()
    user_message = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "create one keyboard order"},
    )
    run = runs.create(session.id, user_message_id=user_message.id)
    run = runs.increment_loop(run.id)
    arguments_json = json.dumps(
        {
            "customer_name": "Alice",
            "product_name": "Keyboard",
            "quantity": 1,
            "unit_price": "99.00",
            "status": None,
        }
    )
    registry = ToolRegistry(create_order_tools(orders, timeout_seconds=1))
    executor = DurableToolExecutor(registry, invocations)
    prepared = await executor.prepare(
        run_id=run.id,
        session_id=session.id,
        step_no=run.loop_count,
        provider_call_id="restart-order-call",
        tool_name="order_create",
        arguments_json=arguments_json,
        tool_call_content={
            "call_id": "restart-order-call",
            "name": "order_create",
            "arguments": arguments_json,
        },
    )
    started = invocations.start_attempt(prepared.record.id)
    assert started.idempotency_key is not None

    first_result = orders.create(
        customer_name="Alice",
        product_name="Keyboard",
        quantity=1,
        unit_price=Decimal("99.00"),
        status=OrderStatus.PENDING,
        idempotency_key=started.idempotency_key,
    )

    recovered_orders = OrderRepository(Database(database_path))
    recovered_registry = ToolRegistry(create_order_tools(recovered_orders, timeout_seconds=1))
    services = build_services(
        Settings(
            database_path=database_path,
            system_prompt_path=_ROOT / "prompts/system.md",
            summarize_prompt_path=_ROOT / "prompts/summarize.md",
            injected_llm_context_window_tokens=10_000,
            agent_shutdown_grace_seconds=0,
        ),
        llm=FakeLLM([final_response()]),
        tools=recovered_registry,
    )

    await services.initialize()
    recovered = await services.supervisor.recover_interrupted()
    task = services.supervisor._tasks[run.id]
    result = await asyncio.wait_for(task, timeout=2)
    await services.close()

    persisted = services.tool_invocations.require(prepared.record.id)
    replayed_orders = services.orders.list()
    tool_results = [
        message
        for message in services.messages.list_after(session.id)
        if message.kind is MessageKind.TOOL_RESULT
    ]
    with services.database.connection() as connection:
        operation_count = connection.execute(
            "SELECT COUNT(*) FROM order_operations WHERE idempotency_key = ?",
            (started.idempotency_key,),
        ).fetchone()[0]

    assert recovered == 1
    assert result.status is RunStatus.COMPLETED, (result.error_code, result.error_message)
    assert persisted.status.value == "succeeded"
    assert persisted.attempt_count == 2
    assert persisted.idempotency_key == started.idempotency_key
    assert len(replayed_orders) == 1
    assert replayed_orders[0].id == first_result.id
    assert operation_count == 1
    assert len(tool_results) == 1
    assert tool_results[0].content["result"]["data"]["id"] == first_result.id
