from decimal import Decimal

import pytest

from minimal_agent.storage import Database, OrderRepository
from minimal_agent.tools import (
    ToolExecutionContext,
    ToolRegistry,
    ToolRetrySemantics,
    ToolSideEffect,
    create_order_tools,
)


def _context(key: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        invocation_id=f"inv-{key}",
        provider_call_id=f"call-{key}",
        idempotency_key=key,
        attempt_no=1,
    )


@pytest.mark.asyncio
async def test_order_tools_cover_crud_and_return_structured_conflicts(database: Database) -> None:
    # 验证订单工具覆盖 CRUD 并返回结构化冲突。
    repository = OrderRepository(database)
    registry = ToolRegistry(create_order_tools(repository))

    created = await registry.execute(
        "order_create",
        {
            "customer_name": "Alice",
            "product_name": "Keyboard",
            "quantity": 2,
            "unit_price": "199.90",
            "status": None,
        },
        context=_context("tool-create-0001"),
    )
    assert created.ok
    assert created.data["total_amount"] == "399.80"
    order_id = created.data["id"]

    conflicting_create = await registry.execute(
        "order_create",
        {
            "customer_name": "Alice",
            "product_name": "Keyboard",
            "quantity": 3,
            "unit_price": "199.90",
            "status": None,
        },
        context=_context("tool-create-0001"),
    )
    assert not conflicting_create.ok
    assert conflicting_create.error.code == "IDEMPOTENCY_CONFLICT"

    fetched = await registry.execute("order_get", {"order_id": order_id})
    listed = await registry.execute("order_list", {"status": None, "limit": None, "offset": None})
    assert fetched.ok and fetched.data["id"] == order_id
    assert listed.ok and listed.data["count"] == 1

    updated = await registry.execute(
        "order_update",
        {
            "order_id": order_id,
            "expected_version": 1,
            "customer_name": None,
            "product_name": None,
            "quantity": 3,
            "unit_price": None,
            "status": "confirmed",
        },
        context=_context("tool-update-0001"),
    )
    assert updated.ok and updated.data["version"] == 2

    stale = await registry.execute(
        "order_update",
        {
            "order_id": order_id,
            "expected_version": 1,
            "customer_name": None,
            "product_name": None,
            "quantity": 4,
            "unit_price": None,
            "status": None,
        },
        context=_context("tool-update-0002"),
    )
    assert not stale.ok
    assert stale.error.code == "ORDER_VERSION_CONFLICT"

    deleted = await registry.execute(
        "order_delete",
        {
            "order_id": order_id,
            "expected_version": 2,
        },
        context=_context("tool-delete-0001"),
    )
    assert deleted.ok and deleted.data["deleted"] is True
    assert repository.list() == []


def test_order_tool_contracts_are_strict_and_declare_write_semantics(database: Database) -> None:
    # 验证订单工具使用严格契约并声明写操作语义。
    tools = {tool.name: tool for tool in create_order_tools(OrderRepository(database))}

    assert tools["order_create"].side_effect is ToolSideEffect.WRITE
    assert tools["order_update"].side_effect is ToolSideEffect.WRITE
    assert tools["order_delete"].side_effect is ToolSideEffect.WRITE
    assert tools["order_get"].side_effect is ToolSideEffect.READ
    assert tools["order_list"].side_effect is ToolSideEffect.READ
    for name, tool in tools.items():
        expected_retry = (
            ToolRetrySemantics.WITH_IDEMPOTENCY_KEY
            if tool.side_effect is ToolSideEffect.WRITE
            else ToolRetrySemantics.SAFE
        )
        assert tool.retry_semantics is expected_retry
        schema = tool.parameters_schema()
        assert set(schema["required"]) == set(schema["properties"]), name


def test_order_repository_accepts_exact_decimal_values(database: Database) -> None:
    # 验证订单仓储接受精确十进制金额。
    record = OrderRepository(database).create(
        customer_name="Alice",
        product_name="Cable",
        quantity=3,
        unit_price=Decimal("0.10"),
        idempotency_key="decimal-key-0001",
    )
    assert record.total_amount == Decimal("0.30")
