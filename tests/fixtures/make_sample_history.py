"""Build tests/fixtures/sample_history.json: a made-up account history for the tests.

    python tests/fixtures/make_sample_history.py

Every ticker, price and quantity here is fictional (QQQ only plays the index). The story is written so the
tests can exercise the rules on realistic data:

  - a cash account funded on 2026-04-01, building positions through the summer (the pre-launch baseline)
  - the test period starts 2026-10-01 with 14 buys, none of them checked first
  - PINE and KITE are added to while down more than 10% (averaging down); both end up on the reduce-only list
  - ORCA ends over its 16% cap; two of its fills on 10-09 come from GTC orders placed the evening before
  - DUNE has no daily bars or earnings dates in the fixture, so its buys hit "missing data"
  - QQQ falls 2.5% on 10-16; three buys that afternoon (KITE, QQQ, MAPL) — only the first is allowed
  - the last buy (MAPL) leaves cash under 5% of NAV
  - OLDC changes its ticker to NEWC in August; the contract id stays the same

Cash, NAV, positions and average costs are all computed from the fills and the price paths below, so the
fixture is consistent with itself. The output is deterministic.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rules.calendar import is_trading_day  # noqa: E402

OUT = Path(__file__).with_name("sample_history.json")
ET = ZoneInfo("America/New_York")
BARS_FROM, LAST_DAY = date(2025, 10, 1), date(2026, 10, 16)
ACCOUNT_FROM = date(2026, 4, 1)
COMMISSION = 1.00
TARGET_CASH = 2_100.00  # cash after the last fill: a little under 5% of NAV

# Closes on anchor days; prices in between are interpolated with a small wiggle.
ANCHORS = {
    "ORCA": {"2025-10-01": 230, "2026-01-15": 210, "2026-04-08": 250, "2026-06-10": 275, "2026-08-18": 290,
             "2026-10-08": 335, "2026-10-09": 328, "2026-10-14": 318, "2026-10-15": 307, "2026-10-16": 300},
    "QQQ": {"2025-10-01": 430, "2026-03-02": 455, "2026-04-15": 470, "2026-05-20": 480, "2026-07-22": 500,
            "2026-10-14": 572, "2026-10-15": 574.36, "2026-10-16": 560},
    "PINE": {"2025-10-01": 190, "2026-02-02": 175, "2026-05-06": 225, "2026-06-24": 235, "2026-08-05": 232,
             "2026-09-15": 240, "2026-10-09": 210, "2026-10-12": 201, "2026-10-14": 191, "2026-10-15": 186,
             "2026-10-16": 180},
    "KITE": {"2025-10-01": 120, "2026-03-16": 112, "2026-04-22": 140, "2026-07-08": 148, "2026-09-10": 150,
             "2026-10-01": 138, "2026-10-16": 120},
    "MAPL": {"2025-10-01": 140, "2026-01-20": 128, "2026-05-13": 160, "2026-09-09": 165, "2026-10-01": 158,
             "2026-10-15": 153, "2026-10-16": 150},
    # no bars exported for these: they only value the account
    "DUNE": {"2025-10-01": 220, "2026-06-03": 240, "2026-10-12": 270, "2026-10-16": 260},
    "OLDC": {"2025-10-01": 26, "2026-05-27": 30, "2026-09-15": 38, "2026-10-16": 40},
    "RIVR": {"2025-10-01": 180, "2026-04-29": 190, "2026-10-05": 205, "2026-10-16": 210},
    "NOVA": {"2025-10-01": 70, "2026-04-10": 75, "2026-10-05": 112, "2026-10-16": 110},
    "SAGE": {"2025-10-01": 140, "2026-05-01": 150, "2026-10-02": 178, "2026-10-15": 182, "2026-10-16": 180},
    "FERN": {"2025-10-01": 45, "2026-05-05": 50, "2026-07-14": 58, "2026-10-16": 60},
}
EXPORTED = ("ORCA", "QQQ", "PINE", "KITE", "MAPL")
CONIDS = {symbol: str(900_000 + n) for n, symbol in enumerate(ANCHORS)}
RENAMES = {"NEWC": "OLDC"}  # new ticker → contract it continues
RENAMED_ON = date(2026, 8, 3)

EARNINGS = {
    "ORCA": ["2026-01-15", "2026-04-16", "2026-07-16", "2026-10-15", "2027-01-14"],
    "PINE": ["2026-02-04", "2026-05-06", "2026-08-05", "2026-11-04"],
    "KITE": ["2026-01-28", "2026-04-29", "2026-07-29", "2026-10-28"],
    "MAPL": ["2026-01-29", "2026-04-30", "2026-07-30", "2026-10-29"],
    "QQQ": [],
}
QUOTE_TYPES = {"ORCA": "EQUITY", "PINE": "EQUITY", "KITE": "EQUITY", "MAPL": "EQUITY", "QQQ": "ETF"}

# (fill time ET, side, ticker, quantity, price, order placed ET or None = same moment)
FILLS = [
    # ── pre-launch baseline ──
    ("2026-04-08 10:05", "BUY", "ORCA", 10, 250.00, None),
    ("2026-04-10 11:20", "BUY", "NOVA", 35, 75.00, None),
    ("2026-04-15 09:45", "BUY", "QQQ", 6, 470.00, None),
    ("2026-04-22 13:10", "BUY", "KITE", 12, 140.00, None),
    ("2026-04-29 10:30", "BUY", "RIVR", 6, 190.00, None),
    ("2026-05-01 14:00", "BUY", "SAGE", 20, 150.00, None),
    ("2026-05-05 10:15", "BUY", "FERN", 10, 50.00, None),
    ("2026-05-06 15:30", "BUY", "PINE", 15, 225.00, None),
    ("2026-05-13 11:00", "BUY", "MAPL", 10, 160.00, None),
    ("2026-05-20 10:40", "BUY", "QQQ", 5, 480.00, None),
    ("2026-05-27 12:25", "BUY", "OLDC", 60, 30.00, None),
    ("2026-06-03 09:50", "BUY", "DUNE", 5, 240.00, None),
    ("2026-06-10 13:35", "BUY", "ORCA", 8, 275.00, None),
    ("2026-06-24 10:05", "BUY", "PINE", 10, 235.00, None),
    ("2026-07-08 14:45", "BUY", "KITE", 10, 148.00, None),
    ("2026-07-14 10:20", "SELL", "FERN", 10, 58.00, None),
    ("2026-07-22 11:30", "BUY", "QQQ", 3, 500.00, None),
    ("2026-08-05 15:10", "BUY", "PINE", 6, 232.00, None),
    ("2026-08-18 10:00", "BUY", "ORCA", 4, 290.00, None),
    ("2026-09-09 13:20", "BUY", "MAPL", 6, 165.00, None),
    ("2026-09-15 10:45", "SELL", "NEWC", 5, 38.00, None),
    # ── test period ──
    ("2026-10-02 09:30", "SELL", "SAGE", 3, 178.00, "2026-09-30 20:05"),
    ("2026-10-05 13:40", "BUY", "RIVR", 2, 205.00, None),
    ("2026-10-05 13:55", "SELL", "NOVA", 5, 112.00, None),
    ("2026-10-09 09:51", "BUY", "ORCA", 2, 330.00, "2026-10-08 18:04"),
    ("2026-10-09 12:21", "BUY", "ORCA", 2, 325.00, "2026-10-08 18:05"),
    ("2026-10-12 10:12", "BUY", "PINE", 3, 200.00, None),
    ("2026-10-12 14:16", "BUY", "DUNE", 2, 270.00, "2026-10-08 18:06"),
    ("2026-10-13 09:40", "BUY", "DUNE", 2, 265.00, "2026-10-08 18:07"),
    ("2026-10-14 12:02", "BUY", "PINE", 3, 190.00, None),
    ("2026-10-15 07:01", "BUY", "ORCA", 2, 310.00, "2026-10-15 05:18"),
    ("2026-10-15 08:54", "BUY", "ORCA", 2, 305.00, "2026-10-15 05:19"),
    ("2026-10-15 09:53", "BUY", "DUNE", 1, 262.00, "2026-10-15 06:12"),
    ("2026-10-15 13:42", "SELL", "SAGE", 2, 182.00, None),
    ("2026-10-15 14:39", "BUY", "PINE", 3, 185.00, None),
    ("2026-10-16 10:00", "SELL", "NEWC", 5, 40.00, None),
    ("2026-10-16 15:44", "BUY", "KITE", 3, 121.00, None),
    ("2026-10-16 15:49", "BUY", "QQQ", 1, 561.50, "2026-10-16 15:48"),
    ("2026-10-16 15:52", "BUY", "MAPL", 4, 151.00, None),
]


def trading_days(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def price_path(symbol: str, days: list[date]) -> dict[date, float]:
    anchors = sorted((date.fromisoformat(k), float(v)) for k, v in ANCHORS[symbol].items())
    phase = sum(map(ord, symbol)) % 7
    out = {}
    for i, day in enumerate(days):
        exact = next((v for d, v in anchors if d == day), None)
        if exact is not None:
            out[day] = exact
            continue
        before = max((a for a in anchors if a[0] < day), default=anchors[0])
        after = min((a for a in anchors if a[0] > day), default=anchors[-1])
        span = (after[0] - before[0]).days or 1
        base = before[1] + (after[1] - before[1]) * (day - before[0]).days / span
        out[day] = round(base * (1 + 0.006 * math.sin(0.9 * i + phase)), 2)
    return out


def utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def contract(symbol: str) -> str:
    return CONIDS[RENAMES.get(symbol, symbol)]


def build() -> dict:
    days = trading_days(BARS_FROM, LAST_DAY)
    prices = {s: price_path(s, days) for s in ANCHORS}
    prices["NEWC"] = prices["OLDC"]

    trades = []
    for n, (when, side, symbol, qty, price, placed) in enumerate(FILLS, start=1):
        gross = -qty * price if side == "BUY" else qty * price
        trades.append({
            "trade_id": f"T{n:04d}", "symbol": symbol, "side": side, "quantity": qty, "price": price,
            "proceeds": round(gross, 2), "commission": -COMMISSION, "executed_at": stamp(utc(when)),
            "order_id": f"O{n:04d}", "order_placed_at": stamp(utc(placed or when)), "conid": contract(symbol),
        })

    # cash: fund the account so cash after the last fill is TARGET_CASH
    flow = sum(t["proceeds"] + t["commission"] for t in trades)
    deposit = round(TARGET_CASH - flow, 2)

    snapshots, positions = [], {}
    by_day: dict[date, list[dict]] = {}
    for t in trades:
        day = datetime.strptime(t["executed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ET).date()
        by_day.setdefault(day, []).append(t)
    cash, held = deposit, {}
    for day in trading_days(ACCOUNT_FROM, LAST_DAY):
        for t in by_day.get(day, []):
            cash += t["proceeds"] + t["commission"]
            lots = held.setdefault(t["conid"], [])
            if t["side"] == "BUY":
                lots.append([t["quantity"], (t["quantity"] * t["price"] + COMMISSION) / t["quantity"]])
            else:
                left = t["quantity"]
                while left > 1e-9:
                    used = min(lots[0][0], left)
                    lots[0][0] -= used
                    left -= used
                    if lots[0][0] <= 1e-9:
                        lots.pop(0)
            positions[t["conid"]] = t["symbol"]
        assert cash > 0, (day, cash)
        value = sum(sum(q for q, _ in lots) * prices[positions[conid]][day] for conid, lots in held.items() if lots)
        snapshots.append([day.isoformat(), round(cash, 2), round(cash + value, 2)])

    final = []
    for conid, lots in held.items():
        qty = sum(q for q, _ in lots)
        if qty <= 1e-9:
            continue
        symbol = positions[conid]
        mark = prices[symbol][LAST_DAY]
        final.append({"symbol": symbol, "quantity": qty, "avg_cost": round(sum(q * c for q, c in lots) / qty, 4),
                      "mark_price": mark, "market_value": round(qty * mark, 2)})
    final.sort(key=lambda p: -p["market_value"])

    bars = {}
    for symbol in EXPORTED:
        rows, previous = [], None
        for day in days:
            close = prices[symbol][day]
            open_ = previous if previous is not None else close
            rows.append([day.isoformat(), open_, round(max(open_, close) * 1.008, 2),
                         round(min(open_, close) * 0.992, 2), close])
            previous = close
        bars[symbol] = rows

    return {
        "about": "Fictional account history for the tests; built by tests/fixtures/make_sample_history.py.",
        "positions_as_of": LAST_DAY.isoformat(),
        "deposit": deposit,
        "trades": trades,
        "snapshots": snapshots,
        "positions": final,
        "bars": bars,
        "earnings": EARNINGS,
        "quote_types": QUOTE_TYPES,
    }


def main() -> None:
    data = build()
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    as_of, cash, nav = data["snapshots"][-1]
    print(f"{OUT.name}: {len(data['trades'])} fills, {len(data['snapshots'])} snapshots, "
          f"{as_of} cash {cash:,.2f} / NAV {nav:,.2f} = {cash / nav:.1%}")
    for p in data["positions"]:
        print(f"  {p['symbol']:<5}{p['quantity']:>5g} @ {p['mark_price']:>8.2f}  cost {p['avg_cost']:>8.2f}  "
              f"{p['market_value'] / nav:6.1%}")


if __name__ == "__main__":
    main()
