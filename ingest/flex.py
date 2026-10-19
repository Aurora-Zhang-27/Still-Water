"""IBKR Flex Web Service: fetch statements and parse the XML.

Read-only: the Flex Web Service can only download statements; it has no trading interface.

Fetching takes two steps:
  1. SendRequest  ask for a statement with token + query ID; get a ReferenceCode and download URL
  2. GetStatement download with the ReferenceCode; while the statement is still being generated it
                  returns 1019, so wait and retry
"""

from __future__ import annotations

import itertools
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from ingest.models import AccountSnapshot, Position, Statement, Trade

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
API_VERSION = "3"
USER_AGENT = "still-water/0.1 (read-only)"
# times in Flex statements are US Eastern
FLEX_TIMEZONE = ZoneInfo("America/New_York")

# these error codes mean "try again later"
RETRYABLE_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}

SECTION_TAGS = ("EquitySummaryInBase", "CashReport", "OpenPositions", "Trades", "TradeConfirms")


class FlexError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(f"[{code}] {message}" if code else message)
        self.code = code


# ── download ───────────────────────────────────────────────


class FlexClient:
    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_seconds: float = 180,
    ):
        if not token:
            raise FlexError("IBKR_FLEX_TOKEN is not set in .env")
        self._token = token
        self._session = session or requests.Session()
        self._sleep = sleep
        self._max_wait = max_wait_seconds

    def fetch(self, query_id: str) -> bytes:
        """Return the raw statement (XML bytes)."""
        reference, url = self._retrying(lambda: self._send_request(query_id))
        return self._retrying(lambda: self._get_statement(url, reference))

    def _retrying(self, call):
        waited, delay = 0.0, 3.0
        while True:
            try:
                return call()
            except FlexError as exc:
                if exc.code not in RETRYABLE_CODES or waited >= self._max_wait:
                    raise
            self._sleep(delay)
            waited += delay
            delay = min(delay * 2, 30)

    def _send_request(self, query_id: str) -> tuple[str, str]:
        body = self._get(SEND_REQUEST_URL, {"t": self._token, "q": query_id, "v": API_VERSION})
        root = _xml(body)
        if root.tag != "FlexStatementResponse":
            raise FlexError(f"SendRequest returned something unexpected: <{root.tag}>")
        if (root.findtext("Status") or "").strip() != "Success":
            raise _error_from(root)
        reference = (root.findtext("ReferenceCode") or "").strip()
        url = (root.findtext("Url") or "").strip()
        if not reference or not url:
            raise FlexError("SendRequest succeeded but ReferenceCode or Url is missing")
        # the token is only ever sent to IBKR's own https host
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".interactivebrokers.com"):
            raise FlexError(f"Download URL is not an IBKR https host; stopped: {parsed.hostname}")
        return reference, url

    def _get_statement(self, url: str, reference: str) -> bytes:
        body = self._get(url, {"t": self._token, "q": reference, "v": API_VERSION})
        if body.lstrip().startswith(b"<"):
            root = _xml(body)
            if root.tag == "FlexStatementResponse":
                raise _error_from(root)
        return body

    def _get(self, url: str, params: dict[str, str]) -> bytes:
        try:
            response = self._session.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=60
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # requests errors include the full URL (with the token); redact it first
            raise FlexError(f"Network request failed: {_redact(str(exc), self._token)}") from None
        return response.content


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _error_from(root: ET.Element) -> FlexError:
    code = (root.findtext("ErrorCode") or "").strip() or None
    message = (root.findtext("ErrorMessage") or root.findtext("Status") or "unknown error").strip()
    return FlexError(message, code)


def _xml(body: bytes | str) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise FlexError("The response is not XML. Set Format to XML in the Flex Query settings") from None


# ── parsing ────────────────────────────────────────────────

_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y")
_TIME_FORMATS = ("%H%M%S", "%H:%M:%S", "%H%M", "%H:%M")


def parse_flex_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise FlexError(f"Unrecognized date format: {value!r}")


def parse_flex_datetime(value: str | None) -> datetime | None:
    """Flex's "20261016;103012" (US Eastern) → UTC. Also accepts "2026-10-16, 10:30:12" and similar."""
    value = (value or "").strip()
    if not value:
        return None
    parts = re.split(r"[;,T ]+", value, maxsplit=1)
    day = parse_flex_date(parts[0])
    clock = None
    if len(parts) == 2 and parts[1]:
        for fmt in _TIME_FORMATS:
            try:
                clock = datetime.strptime(parts[1], fmt).time()
                break
            except ValueError:
                continue
        else:
            raise FlexError(f"Unrecognized time format: {value!r}")
    local = datetime.combine(day, clock or datetime.min.time(), tzinfo=FLEX_TIMEZONE)
    return local.astimezone(timezone.utc)


def _num(el: ET.Element, *names: str) -> float | None:
    for name in names:
        raw = (el.get(name) or "").strip().replace(",", "")
        if raw and raw != "--":
            try:
                return float(raw)
            except ValueError:
                raise FlexError(f"<{el.tag}> {name}={raw!r} is not a number") from None
    return None


def _text(el: ET.Element, *names: str) -> str | None:
    for name in names:
        value = (el.get(name) or "").strip()
        if value:
            return value
    return None


def _mask(account_id: str) -> str:
    return account_id[:1] + "***" + account_id[-3:] if len(account_id) > 4 else "***"


def parse_statements(body: bytes | str, expected_account: str | None = None) -> list[Statement]:
    root = _xml(body)
    if root.tag == "FlexStatementResponse":
        raise _error_from(root)
    if root.tag != "FlexQueryResponse":
        raise FlexError(f"Not a Flex statement: the root element is <{root.tag}>")
    statements = [_parse_one(el) for el in root.iter("FlexStatement")]
    if not statements:
        raise FlexError("The statement has no FlexStatement")
    if expected_account:
        for stmt in statements:
            if stmt.account_id != expected_account:
                raise FlexError(
                    f"Statement account {_mask(stmt.account_id)} does not match IBKR_ACCOUNT_ID "
                    f"{_mask(expected_account)} in .env"
                )
    return statements


def _parse_one(stmt_el: ET.Element) -> Statement:
    account_id = stmt_el.get("accountId") or ""
    to_date = parse_flex_date(stmt_el.get("toDate"))
    sections = {tag for tag in SECTION_TAGS if stmt_el.find(tag) is not None}
    skipped: list[str] = []

    positions = []
    for el in stmt_el.iter("OpenPosition"):
        if (el.get("levelOfDetail") or "SUMMARY").upper() != "SUMMARY":
            continue
        report_date = parse_flex_date(el.get("reportDate")) or to_date
        quantity = _num(el, "position")
        symbol = _text(el, "symbol")
        if report_date is None or quantity is None or symbol is None:
            skipped.append(f"Position row missing ticker, quantity or date: {symbol}")
            continue
        positions.append(
            Position(
                as_of=report_date.isoformat(),
                account_id=el.get("accountId") or account_id,
                symbol=symbol,
                asset_class=_text(el, "assetCategory") or "STK",
                quantity=quantity,
                avg_cost=_num(el, "costBasisPrice"),
                mark_price=_num(el, "markPrice"),
                market_value=_num(el, "positionValue"),
                unrealized_pnl=_num(el, "fifoPnlUnrealized"),
                currency=_text(el, "currency") or "USD",
            )
        )

    trades = []
    for el in itertools.chain(stmt_el.iter("Trade"), stmt_el.iter("TradeConfirm")):
        if (el.get("levelOfDetail") or "EXECUTION").upper() != "EXECUTION":
            continue
        trade = _parse_trade(el, account_id, skipped)
        if trade:
            trades.append(trade)

    snapshots = _parse_snapshots(stmt_el, account_id, to_date, positions)
    if not snapshots and ("CashReport" in sections or "EquitySummaryInBase" in sections):
        skipped.append("Cash section present but cash or NAV could not be read")

    return Statement(
        source="flex",
        account_id=account_id,
        from_date=parse_flex_date(stmt_el.get("fromDate")),
        to_date=to_date,
        positions=positions,
        trades=trades,
        snapshots=snapshots,
        sections=sections,
        skipped=skipped,
    )


def _parse_trade(el: ET.Element, account_id: str, skipped: list[str]) -> Trade | None:
    symbol = _text(el, "symbol") or "?"
    buy_sell = (el.get("buySell") or "").upper()
    trade_id = _text(el, "tradeID")
    if "(CA.)" in buy_sell:
        skipped.append(f"Cancelled fill {symbol} tradeID={trade_id}")
        return None
    if not trade_id:
        exec_id = _text(el, "ibExecID", "execID")
        trade_id = f"exec:{exec_id}" if exec_id else None
    quantity = _num(el, "quantity")
    price = _num(el, "tradePrice", "price")
    when = el.get("dateTime") or " ".join(filter(None, [el.get("tradeDate"), el.get("tradeTime")]))
    executed_at = parse_flex_datetime(when)
    if not trade_id or not quantity or not price or not executed_at:
        skipped.append(f"Trade row missing id, quantity, price or time: {symbol}")
        return None
    if buy_sell.startswith("BUY"):
        side = "BUY"
    elif buy_sell.startswith("SELL"):
        side = "SELL"
    else:
        side = "BUY" if quantity > 0 else "SELL"
    return Trade(
        trade_id=trade_id,
        account_id=el.get("accountId") or account_id,
        symbol=symbol,
        asset_class=_text(el, "assetCategory") or "STK",
        side=side,
        quantity=abs(quantity),
        price=price,
        proceeds=_num(el, "proceeds"),
        commission=_num(el, "ibCommission", "commission"),
        currency=_text(el, "currency") or "USD",
        executed_at=executed_at,
        order_id=_text(el, "ibOrderID", "orderID"),
        order_placed_at=parse_flex_datetime(el.get("orderTime")),
        order_type=_text(el, "orderType"),
        time_in_force=_text(el, "timeInForce"),
        raw=dict(el.attrib),
    )


def _parse_snapshots(
    stmt_el: ET.Element, account_id: str, to_date: date | None, positions: list[Position]
) -> list[AccountSnapshot]:
    """Prefer the daily NAV summary (one row per day); without it, estimate one end-of-period row from
    the cash report + position market values."""
    snapshots = []
    for el in stmt_el.iter("EquitySummaryByReportDateInBase"):
        report_date = parse_flex_date(el.get("reportDate"))
        cash, total = _num(el, "cash"), _num(el, "total")
        if report_date and cash is not None and total and total > 0:
            snapshots.append(
                AccountSnapshot(report_date.isoformat(), el.get("accountId") or account_id,
                                _text(el, "currency") or "USD", cash, total)
            )
    if snapshots or to_date is None:
        return snapshots

    for el in stmt_el.iter("CashReportCurrency"):
        if (el.get("currency") or "").upper() != "BASE_SUMMARY":
            continue
        cash = _num(el, "endingCash")
        if cash is None:
            continue
        holdings = sum(p.market_value or 0 for p in positions if p.as_of == to_date.isoformat())
        nav = cash + holdings
        if nav > 0:
            snapshots.append(AccountSnapshot(to_date.isoformat(), account_id, "USD", cash, nav))
    return snapshots
