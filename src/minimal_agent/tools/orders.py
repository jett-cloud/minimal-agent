"""Agent tools for durable order CRUD operations."""

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from minimal_agent.schemas import OrderRecord, OrderStatus
from minimal_agent.storage import (
    IdempotencyConflictError,
    NotFoundError,
    OrderRepository,
    OrderVersionConflictError,
)
from minimal_agent.tools.base import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
    ToolRetrySemantics,
    ToolSideEffect,
)


class OrderCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_name: Annotated[str, Field(min_length=1, max_length=120)]
    product_name: Annotated[str, Field(min_length=1, max_length=200)]
    quantity: Annotated[int, Field(ge=1, le=100_000)]
    unit_price: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
    status: OrderStatus | None


class OrderGetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: Annotated[str, Field(min_length=1, max_length=128)]


class OrderListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus | None
    limit: Annotated[int, Field(ge=1, le=100)] | None
    offset: Annotated[int, Field(ge=0)] | None


class OrderUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: Annotated[str, Field(min_length=1, max_length=128)]
    expected_version: Annotated[int, Field(ge=1)]
    customer_name: Annotated[str, Field(min_length=1, max_length=120)] | None
    product_name: Annotated[str, Field(min_length=1, max_length=200)] | None
    quantity: Annotated[int, Field(ge=1, le=100_000)] | None
    unit_price: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)] | None
    status: OrderStatus | None

    @model_validator(mode="after")
    def require_change(self) -> "OrderUpdateArguments":
        if all(
            value is None
            for value in (
                self.customer_name,
                self.product_name,
                self.quantity,
                self.unit_price,
                self.status,
            )
        ):
            raise ValueError("At least one order field must be updated")
        return self


class OrderDeleteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: Annotated[str, Field(min_length=1, max_length=128)]
    expected_version: Annotated[int, Field(ge=1)]


def create_order_tools(
    repository: OrderRepository, *, timeout_seconds: float | None = None
) -> tuple[ToolDefinition[Any], ...]:
    def create(arguments: OrderCreateArguments, context: ToolExecutionContext) -> dict[str, Any]:
        try:
            record = repository.create(
                customer_name=arguments.customer_name,
                product_name=arguments.product_name,
                quantity=arguments.quantity,
                unit_price=arguments.unit_price,
                status=arguments.status or OrderStatus.PENDING,
                idempotency_key=_require_key(context),
            )
        except Exception as error:
            _raise_tool_error(error)
        return _record_json(record)

    def get(arguments: OrderGetArguments) -> dict[str, Any]:
        try:
            return _record_json(repository.require(arguments.order_id))
        except NotFoundError as error:
            raise ToolExecutionError("ORDER_NOT_FOUND", "Order was not found") from error

    def list_orders(arguments: OrderListArguments) -> dict[str, Any]:
        records = repository.list(
            status=arguments.status,
            limit=arguments.limit or 20,
            offset=arguments.offset or 0,
        )
        return {"orders": [_record_json(record) for record in records], "count": len(records)}

    def update(arguments: OrderUpdateArguments, context: ToolExecutionContext) -> dict[str, Any]:
        try:
            record = repository.update(
                arguments.order_id,
                expected_version=arguments.expected_version,
                customer_name=arguments.customer_name,
                product_name=arguments.product_name,
                quantity=arguments.quantity,
                unit_price=arguments.unit_price,
                status=arguments.status,
                idempotency_key=_require_key(context),
            )
        except Exception as error:
            _raise_tool_error(error)
        return _record_json(record)

    def delete(arguments: OrderDeleteArguments, context: ToolExecutionContext) -> dict[str, Any]:
        try:
            record = repository.delete(
                arguments.order_id,
                expected_version=arguments.expected_version,
                idempotency_key=_require_key(context),
            )
        except Exception as error:
            _raise_tool_error(error)
        return {"deleted": True, "order": _record_json(record)}

    return (
        ToolDefinition(
            name="order_create",
            description=(
                "Create an order. The runtime supplies the idempotency key for safe retries."
            ),
            args_model=OrderCreateArguments,
            handler=create,
            side_effect=ToolSideEffect.WRITE,
            retry_semantics=ToolRetrySemantics.WITH_IDEMPOTENCY_KEY,
            timeout_seconds=timeout_seconds,
        ),
        ToolDefinition(
            name="order_get",
            description="Get one order by its exact order_id.",
            args_model=OrderGetArguments,
            handler=get,
            side_effect=ToolSideEffect.READ,
            retry_semantics=ToolRetrySemantics.SAFE,
        ),
        ToolDefinition(
            name="order_list",
            description=(
                "List orders, optionally filtered by status, with limit and offset pagination."
            ),
            args_model=OrderListArguments,
            handler=list_orders,
            side_effect=ToolSideEffect.READ,
            retry_semantics=ToolRetrySemantics.SAFE,
        ),
        ToolDefinition(
            name="order_update",
            description=(
                "Update an order using its current expected_version. Pass null for fields that "
                "should remain unchanged; the runtime supplies the idempotency key."
            ),
            args_model=OrderUpdateArguments,
            handler=update,
            side_effect=ToolSideEffect.WRITE,
            retry_semantics=ToolRetrySemantics.WITH_IDEMPOTENCY_KEY,
            timeout_seconds=timeout_seconds,
        ),
        ToolDefinition(
            name="order_delete",
            description=(
                "Delete an order using its current expected_version. The runtime supplies the "
                "idempotency key."
            ),
            args_model=OrderDeleteArguments,
            handler=delete,
            side_effect=ToolSideEffect.WRITE,
            retry_semantics=ToolRetrySemantics.WITH_IDEMPOTENCY_KEY,
            timeout_seconds=timeout_seconds,
        ),
    )


def _record_json(record: OrderRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _require_key(context: ToolExecutionContext) -> str:
    if context.idempotency_key is None:
        raise ToolExecutionError(
            "IDEMPOTENCY_CONTEXT_REQUIRED",
            "Order write requires a framework-managed idempotency key",
        )
    return context.idempotency_key


def _raise_tool_error(error: Exception) -> None:
    if isinstance(error, NotFoundError):
        raise ToolExecutionError("ORDER_NOT_FOUND", "Order was not found") from error
    if isinstance(error, IdempotencyConflictError):
        raise ToolExecutionError(
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used for a different request",
        ) from error
    if isinstance(error, OrderVersionConflictError):
        raise ToolExecutionError(
            "ORDER_VERSION_CONFLICT",
            "The order changed after it was read",
            details={
                "order_id": error.order_id,
                "expected_version": error.expected,
                "actual_version": error.actual,
            },
        ) from error
    raise error
