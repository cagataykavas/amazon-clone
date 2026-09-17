"""Deterministic commerce catalog used by demos and smoke tests."""

from __future__ import annotations

from .repository import CommerceRepository


def seed_demo(repository: CommerceRepository) -> dict[str, int]:
    user_id = repository.add_user("buyer@example.com")
    products = {
        "keyboard": repository.add_product("KEY-001", "Mechanical Keyboard", 8_900, 12),
        "mouse": repository.add_product("MOU-001", "Wireless Mouse", 3_400, 20),
        "dock": repository.add_product("DOC-001", "USB-C Dock", 12_500, 6),
    }
    return {"user_id": user_id, **products}
