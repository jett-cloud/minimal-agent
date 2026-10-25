import asyncio
import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from minimal_agent.schemas import OrderStatus
from minimal_agent.storage import Database, OrderRepository
from minimal_agent.tools import ToolRegistry, create_order_tools
from tests.integration.tools.helpers import run_tool_scenario


def _repository(tmp_path: Path, tool_name: str) -> OrderRepository:
    database = Database(tmp_path / f"{tool_name}.db")
    database.initialize()
    return OrderRepository(database)


def _seed(repository: OrderRepository):
    return repository.create(
        customer_name="Alice",
        product_name="Keyboard",
        quantity=2,
        unit_price=Decimal("199.90"),
        idempotency_key="agent-seed-0001",
    )


@pytest.mark.asyncio
async def test_agent_can_create_order(tmp_path: Path) -> None:
    # 验证 Agent 可以创建订单。
    repository = _repository(tmp_path, "order_create")
    stream, messages, llm = await run_tool_scenario(
        tmp_path,
        tool_name="order_create",
        arguments=json.dumps(
            {
                "customer_name": "Alice",
                "product_name": "Keyboard",
                "quantity": 2,
                "unit_price": "199.90",
                "status": None,
            }
        ),
        registry=ToolRegistry(create_order_tools(repository)),
    )

    assert "event: tool.completed" in stream
    assert messages[2]["content"]["result"]["data"]["version"] == 1
    assert "399.80" in str(llm.requests[1].input)
    assert len(repository.list()) == 1


@pytest.mark.asyncio
async def test_agent_can_get_order(tmp_path: Path) -> None:
    # 验证 Agent 可以查询单个订单。
    repository = _repository(tmp_path, "order_get")
    order = _seed(repository)
    _, messages, _ = await run_tool_scenario(
        tmp_path,
        tool_name="order_get",
        arguments=json.dumps({"order_id": order.id}),
        registry=ToolRegistry(create_order_tools(repository)),
    )

    assert messages[2]["content"]["result"]["data"]["id"] == order.id


@pytest.mark.asyncio
async def test_agent_can_list_orders(tmp_path: Path) -> None:
    # 验证 Agent 可以查询订单列表。
    repository = _repository(tmp_path, "order_list")
    _seed(repository)
    _, messages, _ = await run_tool_scenario(
        tmp_path,
        tool_name="order_list",
        arguments=json.dumps({"status": "pending", "limit": 10, "offset": 0}),
        registry=ToolRegistry(create_order_tools(repository)),
    )

    assert messages[2]["content"]["result"]["data"]["count"] == 1


@pytest.mark.asyncio
async def test_agent_can_update_order(tmp_path: Path) -> None:
    # 验证 Agent 可以更新订单。
    repository = _repository(tmp_path, "order_update")
    order = _seed(repository)
    _, messages, _ = await run_tool_scenario(
        tmp_path,
        tool_name="order_update",
        arguments=json.dumps(
            {
                "order_id": order.id,
                "expected_version": 1,
                "customer_name": None,
                "product_name": None,
                "quantity": 3,
                "unit_price": None,
                "status": "confirmed",
            }
        ),
        registry=ToolRegistry(create_order_tools(repository)),
    )

    result = messages[2]["content"]["result"]["data"]
    assert result["version"] == 2
    assert repository.require(order.id).status is OrderStatus.CONFIRMED


@pytest.mark.asyncio
async def test_agent_can_delete_order(tmp_path: Path) -> None:
    # 验证 Agent 可以删除订单。
    repository = _repository(tmp_path, "order_delete")
    order = _seed(repository)
    _, messages, _ = await run_tool_scenario(
        tmp_path,
        tool_name="order_delete",
        arguments=json.dumps(
            {
                "order_id": order.id,
                "expected_version": 1,
            }
        ),
        registry=ToolRegistry(create_order_tools(repository)),
    )

    assert messages[2]["content"]["result"]["data"]["deleted"] is True
    assert repository.get(order.id) is None


@pytest.mark.asyncio
async def test_agent_retries_timed_out_order_create_without_duplicate(tmp_path: Path) -> None:
    # 验证 Agent 重试超时的创建订单调用时不会重复下单。
    database = Database(tmp_path / "agent-order-timeout.db")
    database.initialize()

    class SlowFirstCreateRepository(OrderRepository):
        def __init__(self, order_database: Database) -> None:
            super().__init__(order_database)
            self._lock = threading.Lock()
            self.attempts = 0
            self.release_first_attempt = threading.Event()
            self.first_attempt_finished = threading.Event()

        def create(self, **kwargs):
            with self._lock:
                self.attempts += 1
                attempt = self.attempts
            if attempt == 1:
                self.release_first_attempt.wait(timeout=5)
            try:
                return super().create(**kwargs)
            finally:
                if attempt == 1:
                    self.first_attempt_finished.set()

    repository = SlowFirstCreateRepository(database)
    stream, _, _ = await run_tool_scenario(
        tmp_path,
        tool_name="order_create",
        arguments=json.dumps(
            {
                "customer_name": "Alice",
                "product_name": "Keyboard",
                "quantity": 1,
                "unit_price": "99.00",
                "status": None,
            }
        ),
        registry=ToolRegistry(create_order_tools(repository, timeout_seconds=0.2)),
    )
    repository.release_first_attempt.set()
    assert await asyncio.to_thread(repository.first_attempt_finished.wait, 2)

    assert "event: tool.retrying" in stream
    assert "event: tool.completed" in stream
    assert repository.attempts == 2
    assert len(repository.list()) == 1


@pytest.mark.asyncio
async def test_agent_retries_when_committed_order_response_times_out(tmp_path: Path) -> None:
    # 验证订单已提交但 handler 报超时时，Agent 收到原调用的成功重放而不会重新规划下单。
    database = Database(tmp_path / "agent-order-committed-timeout.db")
    database.initialize()

    class LostFirstResponseRepository(OrderRepository):
        def __init__(self, order_database: Database) -> None:
            super().__init__(order_database)
            self.attempts = 0
            self.order_ids: list[str] = []

        def create(self, **kwargs):
            self.attempts += 1
            record = super().create(**kwargs)
            self.order_ids.append(record.id)
            if self.attempts == 1:
                raise TimeoutError("response was lost after commit")
            return record

    repository = LostFirstResponseRepository(database)
    stream, messages, llm = await run_tool_scenario(
        tmp_path,
        tool_name="order_create",
        arguments=json.dumps(
            {
                "customer_name": "Alice",
                "product_name": "Monitor",
                "quantity": 1,
                "unit_price": "899.00",
                "status": None,
            }
        ),
        registry=ToolRegistry(create_order_tools(repository, timeout_seconds=1)),
    )

    assert "event: tool.retrying" in stream
    assert "event: tool.completed" in stream
    assert "event: tool.unknown" in stream
    assert repository.attempts == 2
    assert repository.order_ids == [repository.order_ids[0], repository.order_ids[0]]
    assert len(repository.list()) == 1
    assert messages[2]["content"]["result"]["ok"] is True
    assert len(llm.requests) == 2


@pytest.mark.asyncio
async def test_agent_stops_when_both_order_attempts_have_unknown_results(tmp_path: Path) -> None:
    # 验证两次订单调用结果均未知时 Agent 会停止。
    database = Database(tmp_path / "agent-order-unknown.db")
    database.initialize()

    class AlwaysSlowRepository(OrderRepository):
        def __init__(self, order_database: Database) -> None:
            super().__init__(order_database)
            self._lock = threading.Lock()
            self.attempts = 0
            self.finished_attempts = 0
            self.release_attempts = threading.Event()
            self.all_attempts_finished = threading.Event()

        def create(self, **kwargs):
            with self._lock:
                self.attempts += 1
            self.release_attempts.wait(timeout=5)
            try:
                return super().create(**kwargs)
            finally:
                with self._lock:
                    self.finished_attempts += 1
                    if self.finished_attempts == 2:
                        self.all_attempts_finished.set()

    repository = AlwaysSlowRepository(database)
    stream, _, llm = await run_tool_scenario(
        tmp_path,
        tool_name="order_create",
        arguments=json.dumps(
            {
                "customer_name": "Alice",
                "product_name": "Mouse",
                "quantity": 1,
                "unit_price": "29.00",
                "status": None,
            }
        ),
        registry=ToolRegistry(create_order_tools(repository, timeout_seconds=0.05)),
    )
    repository.release_attempts.set()
    assert await asyncio.to_thread(repository.all_attempts_finished.wait, 2)

    assert stream.count("event: tool.unknown") == 2
    assert "TOOL_RESULT_UNKNOWN" in stream
    assert "event: tool.completed" not in stream
    assert len(llm.requests) == 1
    assert repository.attempts == 2
    assert len(repository.list()) == 1
