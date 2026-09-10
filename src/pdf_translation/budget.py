from __future__ import annotations

from decimal import Decimal


class BudgetExceeded(RuntimeError):
    pass


def estimate_cost(input_tokens: int, output_tokens: int, input_rate: float | None, output_rate: float | None) -> float | None:
    if input_rate is None or output_rate is None:
        return None
    value = (Decimal(input_tokens) * Decimal(str(input_rate)) + Decimal(output_tokens) * Decimal(str(output_rate))) / Decimal(1_000_000)
    return float(value)


def reserve_budget(spent: float, reserved: float, requested: float | None, limit: float) -> None:
    if requested is None:
        raise BudgetExceeded("price is unavailable; refusing paid request")
    if spent + reserved + requested > limit:
        raise BudgetExceeded(f"budget exceeded: {spent + reserved + requested:.6f} > {limit:.6f} USD")

