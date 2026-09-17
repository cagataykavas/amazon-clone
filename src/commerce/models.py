"""Validated commerce command models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLine:
    product_id: int
    quantity: int

    def __post_init__(self) -> None:
        if self.product_id <= 0:
            raise ValueError("product_id must be positive")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    status: str
    total_cents: int
    idempotency_key: str
    replayed: bool = False
