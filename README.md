# Commerce Systems Demo

An Amazon-style backend/data-modeling exercise focused on **SQL schema design, inventory consistency, orders, payments, event-driven workflows and analytics** rather than pixel-perfect UI cloning.

## Scope

- PostgreSQL relational model for users, products, inventory, carts, orders and order items
- idempotent order creation boundary
- inventory reservation concept
- payment/order state machine
- event log / outbox pattern
- analytical queries for GMV, conversion and stock health

## Planned stack

FastAPI · PostgreSQL · Redis · Kafka-compatible events · Docker Compose · pytest · SQLAlchemy/Alembic

This repository is intentionally positioned as a systems/data-engineering demo rather than a frontend tutorial clone.
