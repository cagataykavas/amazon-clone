from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from commerce.api import create_app
from commerce.models import OrderLine
from commerce.repository import CommerceRepository, InvalidTransition, InventoryConflict
from commerce.seed import seed_demo


@pytest.fixture
def store(tmp_path):
    values = CommerceRepository(tmp_path / "commerce.db")
    values.initialize()
    identifiers = seed_demo(values)
    return values, identifiers


def test_order_reserves_stock_and_snapshots_price(store):
    repository, identifiers = store
    result = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["keyboard"], 2)], "key-1"
    )
    inventory = {row["product_id"]: row for row in repository.inventory()}
    assert result.status == "pending_payment"
    assert result.total_cents == 17_800
    assert inventory[identifiers["keyboard"]]["reserved"] == 2
    assert inventory[identifiers["keyboard"]]["on_hand"] == 12


def test_duplicate_lines_are_aggregated(store):
    repository, identifiers = store
    result = repository.place_order(
        identifiers["user_id"],
        [OrderLine(identifiers["mouse"], 1), OrderLine(identifiers["mouse"], 2)],
        "key-duplicate-lines",
    )
    assert result.total_cents == 10_200
    row = next(item for item in repository.inventory() if item["sku"] == "MOU-001")
    assert row["reserved"] == 3


def test_order_idempotency_does_not_reserve_twice(store):
    repository, identifiers = store
    lines = [OrderLine(identifiers["dock"], 1)]
    first = repository.place_order(identifiers["user_id"], lines, "key-replay")
    second = repository.place_order(identifiers["user_id"], lines, "key-replay")
    assert second.order_id == first.order_id
    assert second.replayed is True
    row = next(item for item in repository.inventory() if item["sku"] == "DOC-001")
    assert row["reserved"] == 1


def test_insufficient_stock_rolls_back_order(store):
    repository, identifiers = store
    before = repository.metrics()["orders"]
    with pytest.raises(InventoryConflict, match="available"):
        repository.place_order(
            identifiers["user_id"], [OrderLine(identifiers["dock"], 99)], "key-no-stock"
        )
    assert repository.metrics()["orders"] == before


def test_unknown_product_rolls_back_order(store):
    repository, identifiers = store
    with pytest.raises(InventoryConflict, match="unknown products"):
        repository.place_order(identifiers["user_id"], [OrderLine(9999, 1)], "key-unknown")


def test_unknown_user_does_not_leak_reservation(store):
    repository, identifiers = store
    with pytest.raises(sqlite3.IntegrityError):
        repository.place_order(9999, [OrderLine(identifiers["keyboard"], 1)], "bad-user")
    row = next(item for item in repository.inventory() if item["sku"] == "KEY-001")
    assert row["reserved"] == 0


def test_payment_consumes_reserved_inventory(store):
    repository, identifiers = store
    order = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["keyboard"], 2)], "key-pay"
    )
    paid = repository.capture_payment(order.order_id, "payment-1")
    row = next(item for item in repository.inventory() if item["sku"] == "KEY-001")
    assert paid.status == "paid"
    assert row["on_hand"] == 10
    assert row["reserved"] == 0
    assert repository.metrics()["paid_gmv_cents"] == 17_800


def test_payment_capture_is_idempotent(store):
    repository, identifiers = store
    order = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["mouse"], 1)], "key-pay-replay"
    )
    repository.capture_payment(order.order_id, "payment-replay")
    replay = repository.capture_payment(order.order_id, "payment-replay")
    assert replay.replayed is True
    assert len(repository.pending_outbox()) == 2


def test_different_payment_cannot_capture_paid_order(store):
    repository, identifiers = store
    order = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["mouse"], 1)], "key-double-pay"
    )
    repository.capture_payment(order.order_id, "payment-a")
    with pytest.raises(InvalidTransition, match="different payment"):
        repository.capture_payment(order.order_id, "payment-b")


def test_cancellation_releases_reservation(store):
    repository, identifiers = store
    order = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["dock"], 2)], "key-cancel"
    )
    cancelled = repository.cancel_order(order.order_id)
    row = next(item for item in repository.inventory() if item["sku"] == "DOC-001")
    assert cancelled.status == "cancelled"
    assert row["reserved"] == 0
    assert row["on_hand"] == 6


def test_paid_order_cannot_be_cancelled(store):
    repository, identifiers = store
    order = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["mouse"], 1)], "key-paid-cancel"
    )
    repository.capture_payment(order.order_id, "payment-paid")
    with pytest.raises(InvalidTransition, match="cannot cancel"):
        repository.cancel_order(order.order_id)


def test_outbox_follows_state_transitions(store):
    repository, identifiers = store
    order = repository.place_order(
        identifiers["user_id"], [OrderLine(identifiers["mouse"], 1)], "key-events"
    )
    repository.cancel_order(order.order_id)
    assert [event["event_type"] for event in repository.pending_outbox()] == [
        "order.created",
        "order.cancelled",
    ]


def test_api_order_and_payment_flow(tmp_path):
    app = create_app(tmp_path / "api.db")
    identifiers = seed_demo(app.state.repository)
    client = TestClient(app)
    response = client.post(
        "/orders",
        json={
            "user_id": identifiers["user_id"],
            "idempotency_key": "api-order",
            "lines": [{"product_id": identifiers["keyboard"], "quantity": 1}],
        },
    )
    assert response.status_code == 201
    order_id = response.json()["order_id"]
    paid = client.post(f"/orders/{order_id}/capture", json={"payment_id": "api-payment"})
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"


def test_api_maps_inventory_conflict_to_409(tmp_path):
    app = create_app(tmp_path / "api.db")
    identifiers = seed_demo(app.state.repository)
    client = TestClient(app)
    response = client.post(
        "/orders",
        json={
            "user_id": identifiers["user_id"],
            "idempotency_key": "api-conflict",
            "lines": [{"product_id": identifiers["dock"], "quantity": 999}],
        },
    )
    assert response.status_code == 409


def test_order_line_validation():
    with pytest.raises(ValueError, match="quantity"):
        OrderLine(1, 0)
