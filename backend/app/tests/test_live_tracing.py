"""Behaviour the live connectors and the trace loop rely on for speed.

Every case here was a real failure against live chain data, and each fix made
tracing faster. Faster is only acceptable if the answer stays right, so these
pin the correctness half:

  * Etherscan throttles with HTTP 200 + "NOTOK". Unhandled, a throttled lookup
    silently dropped an address and the trace still claimed to be complete.
  * A zero-value "transaction" is a contract call, not a payment - following it
    made the USDT token contract a hop of the money trail.
  * An address that signs transactions is a wallet, so internal transfers can
    never be outgoing from it; fetching them is a wasted rate-limited call.
  * Money that reaches an exchange stops there; walking the exchange's own hot
    wallet traced other customers' funds.

No network and no Neo4j: responses are scripted and the connector is fake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import ingest
from app.services.connectors import live
from app.services.connectors.base import (
    Asset,
    BlockchainConnector,
    ChainTransaction,
    ConnectorError,
    TxIO,
)

WALLET = "0x1111111111111111111111111111111111111111"
FRIEND = "0x2222222222222222222222222222222222222222"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TS = "1767225600"  # 2026-01-01


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(live._ETHERSCAN_PACER, "wait", lambda: None)
    monkeypatch.setattr(live.time, "sleep", lambda _s: None)


def scripted(monkeypatch, responses: dict[str, list[dict]]):
    """Serve Etherscan payloads by `action`, one per call, in order."""
    calls: list[str] = []

    def fake_get_json(_client, _url, params=None, source=None, **_kwargs):
        action = params["action"]
        calls.append(action)
        queue = responses.get(action) or [{"status": "0", "message": "No transactions found"}]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(live, "get_json", fake_get_json)
    return calls


def ok(result):
    return {"status": "1", "message": "OK", "result": result}


def native(frm, to, wei, **extra):
    return {"hash": f"0x{frm[-4:]}{to[-4:]}{wei}", "from": frm, "to": to, "value": str(wei),
            "timeStamp": TS, "blockNumber": "1", "gasUsed": "21000", "gasPrice": "1",
            "isError": "0", "txreceipt_status": "1", **extra}


# ---------------------------------------------------------------------------
# Etherscan connector
# ---------------------------------------------------------------------------
def test_rate_limit_is_retried_not_dropped(monkeypatch):
    throttled = {"status": "0", "message": "NOTOK",
                 "result": "Max calls per sec rate limit reached (3/sec)"}
    scripted(monkeypatch, {"txlist": [throttled, ok([native(WALLET, FRIEND, 10**18)])]})

    txs = live.EtherscanConnector(api_key="k").get_transactions(WALLET, limit=10)

    assert [t.outputs[0].address for t in txs] == [FRIEND]


def test_persistent_rate_limit_raises_instead_of_returning_nothing(monkeypatch):
    # An empty list would read as "this wallet never moved money". Raising lets
    # the trace count the address as a failed lookup.
    throttled = {"status": "0", "message": "NOTOK",
                 "result": "Max calls per sec rate limit reached (3/sec)"}
    scripted(monkeypatch, {"txlist": [throttled]})

    with pytest.raises(ConnectorError, match="rate limit"):
        live.EtherscanConnector(api_key="k").get_transactions(WALLET, limit=10)


def test_zero_value_contract_call_is_not_a_transfer(monkeypatch):
    # An ERC-20 transfer appears in txlist as "0 ETH to the token contract".
    scripted(monkeypatch, {"txlist": [ok([native(WALLET, USDT, 0)])]})

    txs = live.EtherscanConnector(api_key="k").get_transactions(WALLET, limit=10)

    assert all(o.address != USDT for t in txs for o in t.outputs)


def test_failed_transaction_is_still_reported(monkeypatch):
    # Zero-value filtering must not swallow a reverted payment: that record is
    # kept, marked failed, and moves no value downstream.
    reverted = native(WALLET, FRIEND, 0, isError="1", txreceipt_status="0")
    scripted(monkeypatch, {"txlist": [ok([reverted])]})

    txs = live.EtherscanConnector(api_key="k").get_transactions(WALLET, limit=10)

    assert len(txs) == 1 and txs[0].status == "failed"


def test_internal_transfers_skipped_for_a_wallet_that_signs(monkeypatch):
    calls = scripted(monkeypatch, {"txlist": [ok([native(WALLET, FRIEND, 10**18)])]})

    live.EtherscanConnector(api_key="k").get_transactions(WALLET, limit=10)

    assert "txlistinternal" not in calls


def test_internal_transfers_fetched_for_a_possible_contract(monkeypatch):
    # Never seen signing: it may be a contract, whose outgoing value moves as
    # internal transfers - skipping those would lose the trail.
    calls = scripted(monkeypatch, {"txlist": [ok([native(FRIEND, WALLET, 10**18)])]})

    live.EtherscanConnector(api_key="k").get_transactions(WALLET, limit=10)

    assert "txlistinternal" in calls


# ---------------------------------------------------------------------------
# Trace loop
# ---------------------------------------------------------------------------
def pay(frm: str, to: str, txid: str) -> ChainTransaction:
    return ChainTransaction(
        chain="ETH", txid=txid, timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        asset=Asset.native("ETH"),
        inputs=[TxIO(address=frm, value=Decimal(1))],
        outputs=[TxIO(address=to, value=Decimal(1))],
    )


class FakeConnector(BlockchainConnector):
    chain = "ETH"
    source_name = "fake"

    def __init__(self, graph: dict[str, list[ChainTransaction]], failing=()):
        self.graph, self.failing, self.asked = graph, set(failing), []

    def get_transactions(self, address, limit=50):
        self.asked.append(address)
        if address in self.failing:
            raise ConnectorError("indexer refused")
        return self.graph.get(address, [])

    def close(self):
        pass


A, B, C, EXCHANGE = (f"0x{c * 40}" for c in "abce")


def test_trace_stops_at_an_exchange_but_keeps_the_edge_into_it(monkeypatch):
    monkeypatch.setattr(ingest, "_custodial_addresses",
                        lambda chain, addrs: {a for a in addrs if a == EXCHANGE})
    conn = FakeConnector({A: [pay(A, EXCHANGE, "t1")], EXCHANGE: [pay(EXCHANGE, C, "t2")]})

    result = ingest.expand_money_flow(conn, "ETH", A, max_depth=4, max_breadth=10)

    assert EXCHANGE not in conn.asked, "walked into the exchange's own wallet"
    assert "ETH:t1" in {f"{t.chain}:{t.txid}" for t in result["transactions"]}
    assert result["stopped_at_services"] == 1
    assert result["complete"] is True


def test_reported_address_is_traced_even_if_it_is_an_exchange(monkeypatch):
    monkeypatch.setattr(ingest, "_custodial_addresses", lambda chain, addrs: set(addrs))
    conn = FakeConnector({EXCHANGE: [pay(EXCHANGE, B, "t1")]})

    ingest.expand_money_flow(conn, "ETH", EXCHANGE, max_depth=2, max_breadth=10)

    assert conn.asked[0] == EXCHANGE


def test_failed_lookup_makes_the_trace_incomplete(monkeypatch):
    monkeypatch.setattr(ingest, "_custodial_addresses", lambda chain, addrs: set())
    conn = FakeConnector({A: [pay(A, B, "t1"), pay(A, C, "t2")]}, failing={B})

    result = ingest.expand_money_flow(conn, "ETH", A, max_depth=3, max_breadth=10)

    assert result["lookup_failures"] == 1
    assert result["complete"] is False
    assert "did not answer for 1 address" in ingest._coverage_note(result)


def test_concurrent_level_fetch_visits_every_address_once(monkeypatch):
    monkeypatch.setattr(ingest, "_custodial_addresses", lambda chain, addrs: set())
    fan = [f"0x{i:040x}" for i in range(1, 9)]
    conn = FakeConnector({A: [pay(A, f, f"t{i}") for i, f in enumerate(fan)]})

    result = ingest.expand_money_flow(conn, "ETH", A, max_depth=2, max_breadth=20)

    assert sorted(conn.asked) == sorted([A, *fan])
    assert result["addresses_touched"] == 1 + len(fan)


def test_time_budget_stops_the_trace_and_says_so(monkeypatch):
    # Filing a complaint waits on the trace, so it is bounded by the clock,
    # not only by a call count: behind Etherscan's 3 calls/s a 40-address
    # trace needed ~37s. The trace stops at the deadline and reports it.
    monkeypatch.setattr(ingest, "_custodial_addresses", lambda chain, addrs: set())
    monkeypatch.setattr(ingest.settings, "trace_time_budget_seconds", 10.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ingest.time, "monotonic", lambda: clock["t"])

    class SlowConnector(FakeConnector):
        def get_transactions(self, address, limit=50):
            clock["t"] += 11.0  # this one lookup uses up the whole budget
            return super().get_transactions(address, limit)

    conn = SlowConnector({A: [pay(A, B, "t1")], B: [pay(B, C, "t2")]})

    result = ingest.expand_money_flow(conn, "ETH", A, max_depth=4, max_breadth=10)

    assert conn.asked == [A], "kept fetching after the time budget ran out"
    assert result["budget_exhausted"] is True
    assert result["complete"] is False
    assert "ETH:t1" in {f"{t.chain}:{t.txid}" for t in result["transactions"]}
