"""The program must never be able to trade: no order placing, changing or cancelling calls in the source."""

import re

from settings import PROJECT_ROOT

SOURCE_DIRS = ("ingest", "db", "rules", "audit", "app")

FORBIDDEN = re.compile(
    r"placeOrder|place_order|cancelOrder|cancel_order|modifyOrder|whatIfOrder"
    r"|reqGlobalCancel|bracketOrder|submit_order"
    r"|\b(?:Limit|Market|Stop|StopLimit)Order\s*\("
    r"|/iserver/account/[^\s\"']*/orders?\b"
)


def source_files():
    yield from PROJECT_ROOT.glob("*.py")
    for name in SOURCE_DIRS:
        yield from (PROJECT_ROOT / name).rglob("*.py")


def test_no_order_entry_code():
    hits = []
    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert not hits, "Order-related code found:\n" + "\n".join(hits)


def test_guard_catches_order_calls():
    assert FORBIDDEN.search("ib.placeOrder(contract, order)")
    assert FORBIDDEN.search("order = LimitOrder('BUY', 10, 245)")
    assert FORBIDDEN.search("requests.post(f'{base}/iserver/account/{acct}/orders')")
    assert not FORBIDDEN.search("ib.reqAllOpenOrders()")
    assert not FORBIDDEN.search("SELECT * FROM orders")
