"""Validated order domain and API models."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


CustomerName = Annotated[str, Field(min_length=1, max_length=120)]
ProductName = Annotated[str, Field(min_length=1, max_length=200)]
Quantity = Annotated[int, Field(ge=1, le=100_000)]
UnitPrice = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")]


class OrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    customer_name: CustomerName
    product_name: ProductName
    quantity: Quantity
    unit_price: UnitPrice
    total_amount: Decimal
    status: OrderStatus
    version: Annotated[int, Field(ge=1)]
    created_at: datetime
    updated_at: datetime


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_name: CustomerName
    product_name: ProductName
    quantity: Quantity
    unit_price: UnitPrice
    status: OrderStatus = OrderStatus.PENDING


class UpdateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    customer_name: CustomerName | None = None
    product_name: ProductName | None = None
    quantity: Quantity | None = None
    unit_price: UnitPrice | None = None
    status: OrderStatus | None = None

    @model_validator(mode="after")
    def require_change(self) -> "UpdateOrderRequest":
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
