"""FastAPI commands for orders, payment capture, cancellation and inventory."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .models import OrderLine
from .repository import CommerceRepository, InvalidTransition, InventoryConflict


class LineRequest(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000)


class OrderRequest(BaseModel):
    user_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    lines: list[LineRequest] = Field(min_length=1, max_length=100)


class PaymentRequest(BaseModel):
    payment_id: str = Field(min_length=1, max_length=128)


def create_app(database_path: str | Path | None = None) -> FastAPI:
    resolved_path = database_path or os.getenv("COMMERCE_DATABASE", "commerce.db")
    repository = CommerceRepository(resolved_path)
    repository.initialize()
    app = FastAPI(
        title="Commerce Systems Demo",
        version="0.1.0",
        description="Transactional inventory, ordering, payments and outbox events.",
    )
    app.state.repository = repository

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/inventory")
    def inventory() -> list[dict[str, object]]:
        return repository.inventory()

    @app.post("/orders", status_code=status.HTTP_201_CREATED)
    def place_order(payload: OrderRequest) -> dict[str, object]:
        try:
            result = repository.place_order(
                payload.user_id,
                [OrderLine(line.product_id, line.quantity) for line in payload.lines],
                payload.idempotency_key,
            )
        except InventoryConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=404, detail="unknown user") from error
        return result.__dict__

    @app.post("/orders/{order_id}/capture")
    def capture(order_id: int, payload: PaymentRequest) -> dict[str, object]:
        try:
            return repository.capture_payment(order_id, payload.payment_id).__dict__
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/orders/{order_id}/cancel")
    def cancel(order_id: int) -> dict[str, object]:
        try:
            return repository.cancel_order(order_id).__dict__
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/orders/{order_id}")
    def get_order(order_id: int) -> dict[str, object]:
        result = repository.order(order_id)
        if result is None:
            raise HTTPException(status_code=404, detail="order not found")
        return result

    @app.get("/metrics")
    def metrics() -> dict[str, int]:
        return repository.metrics()

    return app


app = create_app()
