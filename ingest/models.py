"""Common structures produced by the ingestion layer.

Flex (and later Gateway) produce these objects; ingest.store writes them to the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Position:
    as_of: str  # Flex: statement date YYYY-MM-DD
    account_id: str
    symbol: str
    asset_class: str
    quantity: float  # negative for shorts
    avg_cost: float | None
    mark_price: float | None
    market_value: float | None
    unrealized_pnl: float | None
    currency: str


@dataclass(frozen=True)
class Trade:
    trade_id: str
    account_id: str
    symbol: str
    asset_class: str
    side: str  # BUY / SELL
    quantity: float  # always positive; see side for the direction
    price: float
    proceeds: float | None
    commission: float | None
    currency: str
    executed_at: datetime  # UTC
    order_id: str | None
    order_placed_at: datetime | None  # UTC
    order_type: str | None
    time_in_force: str | None
    raw: dict[str, str] = field(compare=False, repr=False)


@dataclass(frozen=True)
class AccountSnapshot:
    as_of: str
    account_id: str
    currency: str
    cash: float
    net_liquidation: float


@dataclass
class Statement:
    source: str  # flex / gateway
    account_id: str
    from_date: date | None
    to_date: date | None
    positions: list[Position]
    trades: list[Trade]
    snapshots: list[AccountSnapshot]
    sections: set[str]  # sections present in the statement, to point out what the query is missing
    skipped: list[str]  # skipped rows and why


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: date  # exchange-local date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None
