"""The Flex download flow (simulated network, no real requests)."""

from pathlib import Path

import pytest
import requests

from ingest.flex import FlexClient, FlexError

SAMPLE = (Path(__file__).parent / "fixtures" / "flex_activity_sample.xml").read_bytes()
TOKEN = "tok_secret_123"
GET_URL = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"


def send_ok(url=GET_URL):
    return (
        "<FlexStatementResponse><Status>Success</Status><ReferenceCode>555</ReferenceCode>"
        f"<Url>{url}</Url></FlexStatementResponse>"
    ).encode()


def flex_error(code, message):
    return (
        f"<FlexStatementResponse><Status>Warn</Status><ErrorCode>{code}</ErrorCode>"
        f"<ErrorMessage>{message}</ErrorMessage></FlexStatementResponse>"
    ).encode()


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def client(session, sleeps=None):
    return FlexClient(TOKEN, session=session, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_waits_while_statement_is_generating():
    sleeps = []
    session = FakeSession(send_ok(), flex_error(1019, "Statement generation in progress"), SAMPLE)
    assert client(session, sleeps).fetch("123456") == SAMPLE
    assert sleeps == [3.0]
    send, first_try, second_try = session.calls
    assert send["params"] == {"t": TOKEN, "q": "123456", "v": "3"}
    assert first_try["url"] == GET_URL and first_try["params"]["q"] == "555"
    assert all(call["headers"]["User-Agent"] for call in session.calls)


def test_expired_token_is_not_retried():
    session = FakeSession(
        b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
        b"<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
    )
    with pytest.raises(FlexError) as exc:
        client(session).fetch("123456")
    assert exc.value.code == "1012"
    assert len(session.calls) == 1


def test_gives_up_after_max_wait():
    session = FakeSession(send_ok(), *[flex_error(1019, "in progress")] * 20)
    c = FlexClient(TOKEN, session=session, sleep=lambda s: None, max_wait_seconds=10)
    with pytest.raises(FlexError) as exc:
        c.fetch("123456")
    assert exc.value.code == "1019"


def test_network_error_does_not_leak_token():
    leak = requests.ConnectionError(f"Max retries exceeded with url: /SendRequest?t={TOKEN}&q=123456")
    with pytest.raises(FlexError) as exc:
        client(FakeSession(leak)).fetch("123456")
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_token_only_sent_to_ibkr():
    session = FakeSession(send_ok("https://evil.example.com/GetStatement"))
    with pytest.raises(FlexError, match="IBKR"):
        client(session).fetch("123456")
    assert len(session.calls) == 1  # the token was never sent to that host


def test_missing_token():
    with pytest.raises(FlexError, match="IBKR_FLEX_TOKEN"):
        FlexClient("")
