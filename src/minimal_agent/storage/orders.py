"""Transactional order persistence with durable write idempotency."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from minimal_agent.schemas import OrderRecord, OrderStatus
from minimal_agent.storage.database import Database
from minimal_agent.storage.repositories import NotFoundError, RepositoryError


class IdempotencyConflictError(RepositoryError):
    """Raised when an idempotency key is reused for a different request."""


class OrderVersionConflictError(RepositoryError):
    """Raised when optimistic concurrency detects a stale order version."""

    def __init__(self, order_id: str, *, expected: int, actual: int) -> None:
        super().__init__(f"Order {order_id} has version {actual}, expected {expected}")
        self.order_id = order_id
        self.expected = expected
        self.actual = actual


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _request_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class OrderRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        customer_name: str,
        product_name: str,
        quantity: int,
        unit_price: Decimal,
        status: OrderStatus = OrderStatus.PENDING,
        idempotency_key: str,
    ) -> OrderRecord:
        now = _utc_now()
        order_id = f"order_{uuid4().hex}"
        payload = {
            "customer_name": customer_name,
            "product_name": product_name,
            "quantity": quantity,
            "unit_price": str(unit_price),
            "status": status.value,
        }
        digest = _request_hash(payload)
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, idempotency_key, "create", digest)
            if replay is not None:
                return replay
            connection.execute(
                """
                INSERT INTO orders (
                    id, customer_name, product_name, quantity, unit_price, status,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    order_id,
                    customer_name,
                    product_name,
                    quantity,
                    str(unit_price),
                    status.value,
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            record = self._from_row(row)
            self._save_operation(connection, idempotency_key, "create", digest, record, now)
        return record

    def get(self, order_id: str) -> OrderRecord | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._from_row(row) if row is not None else None

    def require(self, order_id: str) -> OrderRecord:
        record = self.get(order_id)
        if record is None:
            raise NotFoundError(f"Order not found: {order_id}")
        return record

    def list(
        self,
        *,
        status: OrderStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OrderRecord]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Invalid order list limits")
        query = "SELECT * FROM orders"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status.value)
        query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._from_row(row) for row in rows]

    def update(
        self,
        order_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        customer_name: str | None = None,
        product_name: str | None = None,
        quantity: int | None = None,
        unit_price: Decimal | None = None,
        status: OrderStatus | None = None,
    ) -> OrderRecord:
        changes = {
            "customer_name": customer_name,
            "product_name": product_name,
            "quantity": quantity,
            "unit_price": str(unit_price) if unit_price is not None else None,
            "status": status.value if status is not None else None,
        }
        if all(value is None for value in changes.values()):
            raise ValueError("At least one order field must be updated")
        payload = {"order_id": order_id, "expected_version": expected_version, **changes}
        digest = _request_hash(payload)
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, idempotency_key, "update", digest)
            if replay is not None:
                return replay
            current_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if current_row is None:
                raise NotFoundError(f"Order not found: {order_id}")
            current = self._from_row(current_row)
            self._check_version(current, expected_version)
            values = {
                "customer_name": customer_name or current.customer_name,
                "product_name": product_name or current.product_name,
                "quantity": quantity if quantity is not None else current.quantity,
                "unit_price": str(unit_price if unit_price is not None else current.unit_price),
                "status": (status or current.status).value,
            }
            connection.execute(
                """
                UPDATE orders
                SET customer_name = ?, product_name = ?, quantity = ?, unit_price = ?,
                    status = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    values["customer_name"],
                    values["product_name"],
                    values["quantity"],
                    values["unit_price"],
                    values["status"],
                    _timestamp(now),
                    order_id,
                    expected_version,
                ),
            )
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            record = self._from_row(row)
            self._save_operation(connection, idempotency_key, "update", digest, record, now)
        return record

    def delete(
        self,
        order_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> OrderRecord:
        payload = {"order_id": order_id, "expected_version": expected_version}
        digest = _request_hash(payload)
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, idempotency_key, "delete", digest)
            if replay is not None:
                return replay
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Order not found: {order_id}")
            record = self._from_row(row)
            self._check_version(record, expected_version)
            connection.execute(
                "DELETE FROM orders WHERE id = ? AND version = ?",
                (order_id, expected_version),
            )
            self._save_operation(connection, idempotency_key, "delete", digest, record, now)
        return record

    @staticmethod
    def _check_version(record: OrderRecord, expected_version: int) -> None:
        if record.version != expected_version:
            raise OrderVersionConflictError(
                record.id,
                expected=expected_version,
                actual=record.version,
            )

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        idempotency_key: str,
        operation: str,
        request_hash: str,
    ) -> OrderRecord | None:
        row = connection.execute(
            "SELECT * FROM order_operations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                "Idempotency key was already used for a different order operation"
            )
        return OrderRecord.model_validate_json(row["response_json"])

    @staticmethod
    def _save_operation(
        connection: sqlite3.Connection,
        idempotency_key: str,
        operation: str,
        request_hash: str,
        response: OrderRecord,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO order_operations (
                idempotency_key, operation, request_hash, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                operation,
                request_hash,
                response.model_dump_json(),
                _timestamp(created_at),
            ),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OrderRecord:
        unit_price = Decimal(row["unit_price"])
        return OrderRecord(
            id=row["id"],
            customer_name=row["customer_name"],
            product_name=row["product_name"],
            quantity=row["quantity"],
            unit_price=unit_price,
            total_amount=unit_price * row["quantity"],
            status=OrderStatus(row["status"]),
            version=row["version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
