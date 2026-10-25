"""Order CRUD HTTP API."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from minimal_agent.api.errors import APIError
from minimal_agent.dependencies import get_application_services
from minimal_agent.schemas import (
    APIErrorResponse,
    CreateOrderRequest,
    IdempotencyKey,
    OrderRecord,
    OrderStatus,
    UpdateOrderRequest,
)
from minimal_agent.services import ApplicationServices
from minimal_agent.storage import (
    IdempotencyConflictError,
    NotFoundError,
    OrderVersionConflictError,
)

router = APIRouter(prefix="/v1/orders", tags=["orders"])
_ERROR_RESPONSES = {
    404: {"model": APIErrorResponse, "description": "Order not found"},
    409: {"model": APIErrorResponse, "description": "Write conflict"},
    422: {"model": APIErrorResponse, "description": "Request validation failed"},
}


@router.post(
    "",
    response_model=OrderRecord,
    status_code=status.HTTP_201_CREATED,
    responses={409: _ERROR_RESPONSES[409], 422: _ERROR_RESPONSES[422]},
)
async def create_order(
    request: CreateOrderRequest,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> OrderRecord:
    try:
        return await asyncio.to_thread(
            services.orders.create,
            customer_name=request.customer_name,
            product_name=request.product_name,
            quantity=request.quantity,
            unit_price=request.unit_price,
            status=request.status,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as error:
        raise _idempotency_conflict() from error


@router.get("", response_model=list[OrderRecord], responses={422: _ERROR_RESPONSES[422]})
async def list_orders(
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OrderRecord]:
    return await asyncio.to_thread(
        services.orders.list,
        status=order_status,
        limit=limit,
        offset=offset,
    )


@router.get("/{order_id}", response_model=OrderRecord, responses={404: _ERROR_RESPONSES[404]})
async def get_order(
    order_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
) -> OrderRecord:
    order = await asyncio.to_thread(services.orders.get, order_id)
    if order is None:
        raise APIError(404, "ORDER_NOT_FOUND", "Order not found")
    return order


@router.patch(
    "/{order_id}",
    response_model=OrderRecord,
    responses=_ERROR_RESPONSES,
)
async def update_order(
    order_id: str,
    request: UpdateOrderRequest,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> OrderRecord:
    try:
        return await asyncio.to_thread(
            services.orders.update,
            order_id,
            expected_version=request.expected_version,
            customer_name=request.customer_name,
            product_name=request.product_name,
            quantity=request.quantity,
            unit_price=request.unit_price,
            status=request.status,
            idempotency_key=idempotency_key,
        )
    except NotFoundError as error:
        raise APIError(404, "ORDER_NOT_FOUND", "Order not found") from error
    except IdempotencyConflictError as error:
        raise _idempotency_conflict() from error
    except OrderVersionConflictError as error:
        raise _version_conflict(error) from error


@router.delete(
    "/{order_id}",
    response_model=OrderRecord,
    responses=_ERROR_RESPONSES,
)
async def delete_order(
    order_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
    expected_version: Annotated[int, Query(ge=1)],
) -> OrderRecord:
    try:
        return await asyncio.to_thread(
            services.orders.delete,
            order_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except NotFoundError as error:
        raise APIError(404, "ORDER_NOT_FOUND", "Order not found") from error
    except IdempotencyConflictError as error:
        raise _idempotency_conflict() from error
    except OrderVersionConflictError as error:
        raise _version_conflict(error) from error


def _idempotency_conflict() -> APIError:
    return APIError(
        409,
        "IDEMPOTENCY_CONFLICT",
        "Idempotency-Key was already used for a different request",
    )


def _version_conflict(error: OrderVersionConflictError) -> APIError:
    return APIError(
        409,
        "ORDER_VERSION_CONFLICT",
        "The order changed after it was read",
        details={
            "order_id": error.order_id,
            "expected_version": error.expected,
            "actual_version": error.actual,
        },
    )
