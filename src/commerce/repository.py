"""Transactional inventory, ordering, payment and outbox persistence."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import OrderLine, OrderResult

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS inventory (
    product_id INTEGER PRIMARY KEY REFERENCES products(product_id),
    on_hand INTEGER NOT NULL DEFAULT 0 CHECK(on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0 AND reserved <= on_hand)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    status TEXT NOT NULL CHECK(status IN ('pending_payment', 'paid', 'cancelled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    total_cents INTEGER NOT NULL CHECK(total_cents >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
    PRIMARY KEY(order_id, product_id)
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(order_id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    status TEXT NOT NULL CHECK(status = 'captured'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outbox_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox_events(created_at) WHERE published_at IS NULL;
"""


class InventoryConflict(RuntimeError):
    pass


class InvalidTransition(RuntimeError):
    pass


class CommerceRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def add_user(self, email: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO users(email) VALUES (?)", (email,))
            return int(cursor.lastrowid)

    def add_product(self, sku: str, title: str, price_cents: int, on_hand: int) -> int:
        if price_cents < 0 or on_hand < 0:
            raise ValueError("price and inventory must be non-negative")
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO products(sku, title, price_cents) VALUES (?, ?, ?)",
                (sku, title, price_cents),
            )
            product_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO inventory(product_id, on_hand, reserved) VALUES (?, ?, 0)",
                (product_id, on_hand),
            )
            return product_id

    def _existing_order(
        self, connection: sqlite3.Connection, idempotency_key: str
    ) -> OrderResult | None:
        row = connection.execute(
            """SELECT order_id, status, total_cents, idempotency_key
               FROM orders WHERE idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return OrderResult(
            order_id=row["order_id"],
            status=row["status"],
            total_cents=row["total_cents"],
            idempotency_key=row["idempotency_key"],
            replayed=True,
        )

    @staticmethod
    def _aggregate_lines(lines: Iterable[OrderLine]) -> dict[int, int]:
        quantities: dict[int, int] = defaultdict(int)
        for line in lines:
            quantities[line.product_id] += line.quantity
        if not quantities:
            raise ValueError("order requires at least one line")
        return dict(quantities)

    def place_order(
        self, user_id: int, lines: Iterable[OrderLine], idempotency_key: str
    ) -> OrderResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        quantities = self._aggregate_lines(lines)
        with self.transaction() as connection:
            existing = self._existing_order(connection, idempotency_key)
            if existing is not None:
                return existing

            placeholders = ",".join("?" for _ in quantities)
            rows = connection.execute(
                f"""SELECT p.product_id, p.price_cents, p.active,
                           i.on_hand, i.reserved
                    FROM products p JOIN inventory i ON i.product_id = p.product_id
                    WHERE p.product_id IN ({placeholders})""",
                tuple(quantities),
            ).fetchall()
            products = {int(row["product_id"]): row for row in rows}
            if products.keys() != quantities.keys():
                missing = sorted(quantities.keys() - products.keys())
                raise InventoryConflict(f"unknown products: {missing}")

            total_cents = 0
            for product_id, quantity in quantities.items():
                product = products[product_id]
                available = int(product["on_hand"]) - int(product["reserved"])
                if not product["active"] or available < quantity:
                    raise InventoryConflict(
                        f"product {product_id} requested={quantity} available={available}"
                    )
                total_cents += int(product["price_cents"]) * quantity

            cursor = connection.execute(
                """INSERT INTO orders(user_id, status, idempotency_key, total_cents)
                   VALUES (?, 'pending_payment', ?, ?)""",
                (user_id, idempotency_key, total_cents),
            )
            order_id = int(cursor.lastrowid)
            for product_id, quantity in quantities.items():
                connection.execute(
                    """INSERT INTO order_items(order_id, product_id, quantity, unit_price_cents)
                       VALUES (?, ?, ?, ?)""",
                    (order_id, product_id, quantity, products[product_id]["price_cents"]),
                )
                connection.execute(
                    "UPDATE inventory SET reserved = reserved + ? WHERE product_id = ?",
                    (quantity, product_id),
                )
            self._write_outbox(
                connection,
                f"order:{order_id}:created",
                order_id,
                "order.created",
                {"order_id": order_id, "user_id": user_id, "total_cents": total_cents},
            )
            return OrderResult(order_id, "pending_payment", total_cents, idempotency_key)

    def capture_payment(self, order_id: int, payment_id: str) -> OrderResult:
        with self.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise KeyError(f"order {order_id} not found")
            existing_payment = connection.execute(
                "SELECT payment_id FROM payments WHERE order_id = ?", (order_id,)
            ).fetchone()
            if order["status"] == "paid" and existing_payment is not None:
                if existing_payment["payment_id"] != payment_id:
                    raise InvalidTransition("order already paid with a different payment")
                return OrderResult(
                    order_id, "paid", order["total_cents"], order["idempotency_key"], True
                )
            if order["status"] != "pending_payment":
                raise InvalidTransition(f"cannot pay order in {order['status']} state")

            items = connection.execute(
                "SELECT product_id, quantity FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchall()
            for item in items:
                connection.execute(
                    """UPDATE inventory
                       SET on_hand = on_hand - ?, reserved = reserved - ?
                       WHERE product_id = ?""",
                    (item["quantity"], item["quantity"], item["product_id"]),
                )
            connection.execute(
                """INSERT INTO payments(payment_id, order_id, amount_cents, status)
                   VALUES (?, ?, ?, 'captured')""",
                (payment_id, order_id, order["total_cents"]),
            )
            connection.execute(
                """UPDATE orders SET status = 'paid', updated_at = CURRENT_TIMESTAMP
                   WHERE order_id = ?""",
                (order_id,),
            )
            self._write_outbox(
                connection,
                f"order:{order_id}:paid",
                order_id,
                "order.paid",
                {"order_id": order_id, "payment_id": payment_id},
            )
            return OrderResult(order_id, "paid", order["total_cents"], order["idempotency_key"])

    def cancel_order(self, order_id: int) -> OrderResult:
        with self.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise KeyError(f"order {order_id} not found")
            if order["status"] == "cancelled":
                return OrderResult(
                    order_id, "cancelled", order["total_cents"], order["idempotency_key"], True
                )
            if order["status"] != "pending_payment":
                raise InvalidTransition(f"cannot cancel order in {order['status']} state")
            items = connection.execute(
                "SELECT product_id, quantity FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchall()
            for item in items:
                connection.execute(
                    "UPDATE inventory SET reserved = reserved - ? WHERE product_id = ?",
                    (item["quantity"], item["product_id"]),
                )
            connection.execute(
                """UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                   WHERE order_id = ?""",
                (order_id,),
            )
            self._write_outbox(
                connection,
                f"order:{order_id}:cancelled",
                order_id,
                "order.cancelled",
                {"order_id": order_id},
            )
            return OrderResult(
                order_id, "cancelled", order["total_cents"], order["idempotency_key"]
            )

    @staticmethod
    def _write_outbox(
        connection: sqlite3.Connection,
        event_id: str,
        order_id: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO outbox_events(
                   event_id, aggregate_type, aggregate_id, event_type, payload
               ) VALUES (?, 'order', ?, ?, ?)""",
            (event_id, str(order_id), event_type, json.dumps(payload, sort_keys=True)),
        )

    def inventory(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.product_id, p.sku, p.title, p.price_cents,
                          i.on_hand, i.reserved, i.on_hand - i.reserved AS available
                   FROM products p JOIN inventory i ON i.product_id = p.product_id
                   ORDER BY p.product_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def order(self, order_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return dict(row) if row else None

    def pending_outbox(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM outbox_events WHERE published_at IS NULL
                   ORDER BY sequence"""
            ).fetchall()
        return [dict(row) for row in rows]

    def metrics(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS orders,
                          COALESCE(SUM(CASE WHEN status = 'paid' THEN total_cents ELSE 0 END), 0)
                              AS paid_gmv_cents,
                          SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders
                   FROM orders"""
            ).fetchone()
        return dict(row)
