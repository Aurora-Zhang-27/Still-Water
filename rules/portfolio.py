"""Replay fills to get positions and cost at any moment.

Flex only reports end-of-period positions, but the post-trade audit needs positions and unrealized P&L
"at fill time", so fills are replayed FIFO from the very first one.
Cost includes commissions, matching costBasisPrice in IBKR statements (tests/test_sample_history.py checks it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

_EPS = 1e-9


@dataclass(frozen=True)
class Fill:
    trade_id: str
    symbol: str
    side: str  # BUY / SELL
    quantity: float
    price: float
    commission: float | None
    proceeds: float | None
    executed_at: datetime
    order_id: str | None
    contract_id: str | None = None  # IBKR conid; stays the same when a ticker changes

    @property
    def sort_key(self) -> tuple[datetime, str]:
        return self.executed_at, self.trade_id

    @property
    def cash_effect(self) -> float:
        """Effect on cash: negative for buys, positive for sells, commissions included."""
        gross = self.proceeds if self.proceeds is not None else (
            -self.quantity * self.price if self.side == "BUY" else self.quantity * self.price
        )
        return gross + (self.commission or 0)


@dataclass
class Lot:
    quantity: float
    unit_cost: float


@dataclass
class Holding:
    symbol: str
    lots: list[Lot] = field(default_factory=list)

    @property
    def quantity(self) -> float:
        return sum(lot.quantity for lot in self.lots)

    @property
    def avg_cost(self) -> float | None:
        qty = self.quantity
        return sum(l.quantity * l.unit_cost for l in self.lots) / qty if qty > _EPS else None


def replay(fills: Iterable[Fill]) -> tuple[dict[str, Holding], list[str]]:
    """Returns (ticker → holding, anomalies).

    Fills are merged by contract id (a ticker change such as OLD → NEW stays one holding), shown under
    the ticker of the latest fill. Selling more than is held zeroes the position and records an anomaly.
    """
    holdings: dict[str, Holding] = {}
    anomalies: list[str] = []
    for fill in sorted(fills, key=lambda f: f.sort_key):
        holding = holdings.setdefault(fill.contract_id or fill.symbol, Holding(fill.symbol))
        holding.symbol = fill.symbol
        if fill.side == "BUY":
            unit = (fill.quantity * fill.price + abs(fill.commission or 0)) / fill.quantity
            holding.lots.append(Lot(fill.quantity, unit))
            continue
        remaining = fill.quantity
        while remaining > _EPS and holding.lots:
            lot = holding.lots[0]
            used = min(lot.quantity, remaining)
            lot.quantity -= used
            remaining -= used
            if lot.quantity <= _EPS:
                holding.lots.pop(0)
        if remaining > _EPS:
            anomalies.append(
                f"{fill.symbol} sold {fill.quantity:g} shares on {fill.executed_at:%Y-%m-%d}, "
                f"{remaining:g} more than the replayed position (earlier fills may be missing)"
            )
    return {h.symbol: h for h in holdings.values() if h.quantity > _EPS}, anomalies
