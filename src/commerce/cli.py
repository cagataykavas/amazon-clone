"""Run a complete order lifecycle and print its evidence as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import OrderLine
from .repository import CommerceRepository
from .seed import seed_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="Transactional commerce demo")
    parser.add_argument("--database", type=Path, default=Path("commerce.db"))
    values = parser.parse_args()
    repository = CommerceRepository(values.database)
    repository.initialize()
    identifiers = seed_demo(repository)
    order = repository.place_order(
        identifiers["user_id"],
        [OrderLine(identifiers["keyboard"], 1), OrderLine(identifiers["mouse"], 2)],
        "demo-order-001",
    )
    paid = repository.capture_payment(order.order_id, "demo-payment-001")
    print(
        json.dumps(
            {
                "order": paid.__dict__,
                "inventory": repository.inventory(),
                "metrics": repository.metrics(),
                "outbox": repository.pending_outbox(),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
