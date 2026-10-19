"""Valuation lines: multiples and starting EPS come from valuation_seed.csv; EPS is updated weekly by
yfinance's revision ratio.

Four lines = multiple × base (forward EPS; book value per share for cyclicals):
  pressure line = normal multiple × valuation.pressure_multiple_of_normal × base
  cheap, normal and rich lines = their multiple × base

Weekly updates keep your EPS basis and only apply the change in yfinance's comparable number:
  new EPS = old EPS × (yfinance now / yfinance value stored last time)
Switching straight to yfinance's number would make the lines jump (the two bases can differ, e.g.
8.40 vs 9.10 for the same stock).

Each update appends a row (by date) and keeps history: the audit uses the lines of the fill's week.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db.database import utc_text
from ingest.market import MarketData
from rules.config import RuleBook

# column-name aliases (case-insensitive; spaces, underscores, dashes and notes in parentheses are ignored)
COLUMNS = {
    "symbol": ("symbol", "ticker"),
    "cheap": ("cheap", "cheapmult", "cheapmultiple", "cheappe"),
    "normal": ("normal", "normalmult", "normalmultiple", "normalpe"),
    "rich": ("rich", "richmult", "richmultiple", "expensive", "expensivemult"),
    "base": ("forwardeps", "fwdeps", "eps", "base", "bvps", "bookvalue"),
    "metric": ("metric", "type"),
    "stress": ("stress", "stressmult", "pressure", "pressuremult"),
    "as_of": ("epsasof", "asof", "date"),
}
REQUIRED = ("symbol", "cheap", "normal", "rich", "base")
# lines update weekly; only once this many days have passed, tolerating a job running a day early or late
UPDATE_AFTER_DAYS = 6
# an EPS change larger than this in one week is listed in the weekly report (maybe a fiscal-year rollover)
BIG_CHANGE = 0.10


class SeedError(ValueError):
    pass


@dataclass(frozen=True)
class SeedRow:
    symbol: str
    metric: str  # pe / pb
    base_value: float
    cheap: float
    normal: float
    rich: float
    as_of: date | None = None


def _key(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name or "")
    return re.sub(r"[\s_\-]", "", name).lower()


def compute_lines(book: RuleBook, base: float, cheap: float, normal: float, rich: float) -> dict[str, float]:
    return {
        "pressure": normal * book.valuation.pressure_multiple_of_normal * base,
        "cheap": cheap * base,
        "normal": normal * base,
        "rich": rich * base,
    }


def parse_seed(path: Path, book: RuleBook) -> tuple[list[SeedRow], list[str]]:
    """Return (rows, notes). On an error, say exactly which row and column."""
    text = path.read_text(encoding="utf-8-sig")  # CSVs exported from Excel often start with a BOM
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise SeedError(f"{path.name} is empty")
    lookup = {_key(h): h for h in reader.fieldnames}
    found = {}
    for field, aliases in COLUMNS.items():
        header = next((lookup[a] for a in aliases if a in lookup), None)
        if header:
            found[field] = header
    missing = [f for f in REQUIRED if f not in found]
    if missing:
        names = {"symbol": "symbol", "cheap": "cheap", "normal": "normal", "rich": "rich", "base": "forward_eps"}
        raise SeedError(f"{path.name} is missing columns: {', '.join(names[m] for m in missing)}"
                        f" (columns present: {', '.join(reader.fieldnames)})")

    factor = book.valuation.pressure_multiple_of_normal
    rows, notes, seen = [], [], set()
    for line, raw in enumerate(reader, start=2):
        symbol = (raw.get(found["symbol"]) or "").strip().upper()
        if not symbol:
            continue
        where = f"Row {line} {symbol}"
        if symbol in seen:
            raise SeedError(f"{where}: duplicate ticker")
        seen.add(symbol)

        def number(field: str) -> float:
            cell = (raw.get(found[field]) or "").strip().replace(",", "")
            try:
                return float(cell)
            except ValueError:
                raise SeedError(f"{where}: {found[field]} = {cell!r} is not a number") from None

        base, cheap, normal, rich = (number(f) for f in ("base", "cheap", "normal", "rich"))
        if base <= 0:
            raise SeedError(f"{where}: EPS must be greater than 0 (a loss-making company cannot be valued by multiples)")
        if not 0 < cheap < normal < rich:
            raise SeedError(f"{where}: multiples must satisfy 0 < cheap < normal < rich")
        if "stress" in found and (raw.get(found["stress"]) or "").strip():
            stress = number("stress")
            if abs(stress - normal * factor) > 0.01 * normal:
                notes.append(f"{symbol}: stress multiple {stress:g} does not match the rule's normal × {factor:g} = "
                             f"{normal * factor:g}; using the rule")

        as_of = None
        if "as_of" in found and (raw.get(found["as_of"]) or "").strip():
            try:
                as_of = date.fromisoformat(raw[found["as_of"]].strip())
            except ValueError:
                raise SeedError(f"{where}: date {raw[found['as_of']]!r} should be YYYY-MM-DD") from None

        cyclical = book.valuation.is_cyclical(symbol)
        metric = (raw.get(found["metric"]) or "").strip().lower() if "metric" in found else ""
        metric = metric or (book.valuation.cyclical_metric if cyclical else "pe")
        if metric not in ("pe", "pb"):
            raise SeedError(f"{where}: metric must be pe or pb, got {metric!r}")
        if cyclical and metric != book.valuation.cyclical_metric:
            notes.append(f"{symbol} is cyclical and the rules require {book.valuation.cyclical_metric}, "
                         f"but this row is {metric}: the buy check will treat \"PE not high\" as missing data")
        rows.append(SeedRow(symbol, metric, base, cheap, normal, rich, as_of))
    if not rows:
        raise SeedError(f"{path.name} has no data rows")
    return rows, notes


def _reference(md: MarketData, symbol: str, metric: str, force: bool = False) -> tuple[float | None, str]:
    """yfinance's comparable number: forward EPS for PE, book value per share for PB.
    Returns (value, reason when unavailable)."""
    status = md.refresh(symbol, kinds=["fundamentals"], force=force).get("fundamentals", "")
    field = "forward_eps" if metric == "pe" else "book_value_per_share"
    value = (md.latest(symbol, "fundamentals") or {}).get(field)
    if value is None or value <= 0:
        return None, status if status.startswith("error") else f"yfinance has no {field}"
    return value, ""


def _write(conn: sqlite3.Connection, book: RuleBook, row: SeedRow, as_of: date, source: str,
           reference: float | None) -> None:
    lines = compute_lines(book, row.base_value, row.cheap, row.normal, row.rich)
    conn.execute(
        """INSERT INTO valuation (symbol, as_of, metric, base_value, cheap_multiple, normal_multiple, rich_multiple,
                                  pressure_line, cheap_line, normal_line, rich_line, source, reference_value,
                                  reference_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (symbol, as_of) DO UPDATE SET
               metric = excluded.metric, base_value = excluded.base_value,
               cheap_multiple = excluded.cheap_multiple, normal_multiple = excluded.normal_multiple,
               rich_multiple = excluded.rich_multiple, pressure_line = excluded.pressure_line,
               cheap_line = excluded.cheap_line, normal_line = excluded.normal_line,
               rich_line = excluded.rich_line, source = excluded.source,
               reference_value = excluded.reference_value, reference_at = excluded.reference_at""",
        (row.symbol, as_of.isoformat(), row.metric, row.base_value, row.cheap, row.normal, row.rich,
         lines["pressure"], lines["cheap"], lines["normal"], lines["rich"], source,
         reference, utc_text(datetime.now(timezone.utc)) if reference is not None else None),
    )


@dataclass(frozen=True)
class Imported:
    row: SeedRow
    as_of: date
    reference: float | None  # yfinance value stored alongside; None if unavailable (stored at the first weekly update)


def import_seed(
    conn: sqlite3.Connection, book: RuleBook, path: Path, md: MarketData, today: date
) -> tuple[list[Imported], list[str]]:
    """Import the seed file. Uses the EPS date in the file, else today. Re-importing on the same day
    overwrites that day's rows."""
    rows, notes = parse_seed(path, book)
    imported = []
    for row in rows:
        reference, why = _reference(md, row.symbol, row.metric)
        if reference is None:
            notes.append(f"{row.symbol}: {why}; revisions will be tracked from the first weekly update")
        as_of = row.as_of or today
        with conn:
            _write(conn, book, row, as_of, "seed", reference)
        imported.append(Imported(row, as_of, reference))
    return imported, notes


@dataclass(frozen=True)
class EpsChange:
    symbol: str
    old: float
    new: float | None  # None: not updated, the previous row still applies
    note: str = ""

    @property
    def change(self) -> float | None:
        return self.new / self.old - 1 if self.new is not None else None


def latest_rows(conn: sqlite3.Connection, as_of: date | None = None) -> list[sqlite3.Row]:
    """The latest valuation row per stock, up to as_of (no limit by default)."""
    return conn.execute(
        """SELECT v.* FROM valuation v
           JOIN (SELECT symbol, MAX(as_of) AS as_of FROM valuation WHERE as_of <= ? GROUP BY symbol) m
             ON m.symbol = v.symbol AND m.as_of = v.as_of
           ORDER BY v.symbol""",
        ((as_of or date.max).isoformat(),),
    ).fetchall()


def update_eps(
    conn: sqlite3.Connection, book: RuleBook, md: MarketData, today: date, force: bool = False
) -> list[EpsChange]:
    """Once a week: multiples carry over from the latest row; EPS moves by yfinance's change ratio."""
    changes = []
    for latest in latest_rows(conn):
        symbol, old = latest["symbol"], latest["base_value"]
        if not force and today - date.fromisoformat(latest["as_of"]) < timedelta(days=UPDATE_AFTER_DAYS):
            continue
        reference, why = _reference(md, symbol, latest["metric"], force=force)
        row = SeedRow(symbol, latest["metric"], old, latest["cheap_multiple"], latest["normal_multiple"],
                      latest["rich_multiple"])
        if reference is None:
            changes.append(EpsChange(symbol, old, None, f"{why}; keeping the lines from {latest['as_of']}"))
            continue
        if latest["reference_value"]:
            new = old * reference / latest["reference_value"]
            note = ""
        else:
            new, note = old, "Started tracking yfinance revisions; EPS unchanged this week"
        with conn:
            _write(conn, book, replace(row, base_value=new), today, "yfinance", reference)
        changes.append(EpsChange(symbol, old, new, note))
    return changes


ZONES = ("Above rich", "Normal–rich", "Cheap–normal", "Below cheap")


def zone(price: float, lines: dict[str, float]) -> str:
    """Which band the price is in, split by the cheap / normal / rich lines; the pressure line can sit
    above the cheap line and is looked at separately."""
    if price > lines["rich"]:
        return ZONES[0]
    if price > lines["normal"]:
        return ZONES[1]
    if price > lines["cheap"]:
        return ZONES[2]
    return ZONES[3]
