import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from minimal_agent.schemas import OrderStatus
from minimal_agent.storage import (
    Database,
    IdempotencyConflictError,
    OrderRepository,
    OrderVersionConflictError,
)


def _create(repository: OrderRepository, key: str = "create-key-0001"):
    return repository.create(
        customer_name="Alice",
        product_name="Keyboard",
        quantity=2,
        unit_price=Decimal("199.90"),
        status=OrderStatus.PENDING,
        idempotency_key=key,
    )


def test_order_crud_uses_versions_and_filters(database: Database) -> None:
    # 验证订单 CRUD 使用版本控制和筛选条件。
    repository = OrderRepository(database)
    created = _create(repository)

    assert created.total_amount == Decimal("399.80")
    assert created.version == 1
    assert repository.require(created.id) == created
    assert repository.list(status=OrderStatus.PENDING) == [created]
    assert repository.list(status=OrderStatus.SHIPPED) == []

    updated = repository.update(
        created.id,
        expected_version=1,
        quantity=3,
        status=OrderStatus.CONFIRMED,
        idempotency_key="update-key-0001",
    )
    assert updated.quantity == 3
    assert updated.status is OrderStatus.CONFIRMED
    assert updated.version == 2
    assert updated.total_amount == Decimal("599.70")

    deleted = repository.delete(
        created.id,
        expected_version=2,
        idempotency_key="delete-key-0001",
    )
    assert deleted == updated
    assert repository.get(created.id) is None


def test_write_idempotency_replays_lost_responses_and_rejects_key_reuse(
    database: Database,
) -> None:
    # 验证写操作可重放丢失响应并拒绝冲突的幂等键复用。
    repository = OrderRepository(database)
    first = _create(repository, "uncertain-create-0001")

    replayed = _create(repository, "uncertain-create-0001")

    assert replayed == first
    assert repository.list() == [first]
    with pytest.raises(IdempotencyConflictError):
        repository.create(
            customer_name="Bob",
            product_name="Mouse",
            quantity=1,
            unit_price=Decimal("50.00"),
            idempotency_key="uncertain-create-0001",
        )

    updated = repository.update(
        first.id,
        expected_version=1,
        quantity=4,
        idempotency_key="uncertain-update-0001",
    )
    assert (
        repository.update(
            first.id,
            expected_version=1,
            quantity=4,
            idempotency_key="uncertain-update-0001",
        )
        == updated
    )
    deleted = repository.delete(
        first.id,
        expected_version=2,
        idempotency_key="uncertain-delete-0001",
    )
    assert (
        repository.delete(
            first.id,
            expected_version=2,
            idempotency_key="uncertain-delete-0001",
        )
        == deleted
    )


def test_concurrent_duplicate_create_returns_one_order(database: Database) -> None:
    # 验证并发重复创建只返回一笔订单。
    repository = OrderRepository(database)

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(
            executor.map(lambda _index: _create(repository, "concurrent-key-0001"), range(8))
        )

    assert len({record.id for record in records}) == 1
    assert len(repository.list()) == 1


def test_stale_order_version_is_rejected(database: Database) -> None:
    # 验证过期订单版本会被拒绝。
    repository = OrderRepository(database)
    created = _create(repository)
    repository.update(
        created.id,
        expected_version=1,
        quantity=3,
        idempotency_key="update-key-0001",
    )

    with pytest.raises(OrderVersionConflictError) as caught:
        repository.update(
            created.id,
            expected_version=1,
            quantity=4,
            idempotency_key="update-key-0002",
        )

    assert caught.value.actual == 2
    assert repository.require(created.id).quantity == 3


def test_concurrent_updates_allow_one_version_winner(database: Database) -> None:
    # 验证并发更新仅允许一个版本胜出。
    repository = OrderRepository(database)
    created = _create(repository)

    def update(quantity: int) -> str:
        try:
            repository.update(
                created.id,
                expected_version=1,
                quantity=quantity,
                idempotency_key=f"concurrent-update-{quantity:04d}",
            )
        except OrderVersionConflictError:
            return "conflict"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, (3, 4)))

    assert sorted(outcomes) == ["conflict", "updated"]
    assert repository.require(created.id).version == 2


def test_order_and_idempotency_record_roll_back_together(database: Database) -> None:
    # 验证订单和幂等记录在同一事务中回滚。
    repository = OrderRepository(database)
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_order_operation
            BEFORE INSERT ON order_operations
            BEGIN
                SELECT RAISE(ABORT, 'test operation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test operation failure"):
        _create(repository)

    assert repository.list() == []
    with database.connection() as connection:
        operation_count = connection.execute("SELECT COUNT(*) FROM order_operations").fetchone()[0]
        connection.execute("DROP TRIGGER reject_order_operation")
    assert operation_count == 0


def test_update_and_delete_roll_back_when_idempotency_record_fails(database: Database) -> None:
    # 验证幂等记录写入失败时更新和删除都会回滚。
    repository = OrderRepository(database)
    created = _create(repository)
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_update_operation
            BEFORE INSERT ON order_operations
            WHEN NEW.operation = 'update'
            BEGIN
                SELECT RAISE(ABORT, 'test update operation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test update operation failure"):
        repository.update(
            created.id,
            expected_version=1,
            quantity=3,
            idempotency_key="rollback-update-0001",
        )
    assert repository.require(created.id) == created

    with database.connection() as connection:
        connection.execute("DROP TRIGGER reject_update_operation")
        connection.execute(
            """
            CREATE TRIGGER reject_delete_operation
            BEFORE INSERT ON order_operations
            WHEN NEW.operation = 'delete'
            BEGIN
                SELECT RAISE(ABORT, 'test delete operation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test delete operation failure"):
        repository.delete(
            created.id,
            expected_version=1,
            idempotency_key="rollback-delete-0001",
        )
    assert repository.require(created.id) == created
