"""Live indexer connectors: Etherscan (ETH), TronGrid (TRON), Blockchair (BTC).

Used only when DEMO_MODE=false and the corresponding API key is configured.
Deliberately no full nodes - these are public indexer APIs.

Each connector normalises into the same ChainTransaction shape as the synthetic
connector. Account-model chains (ETH, TRON) produce one input and one output per
transfer; Blockchair returns real UTXO input/output sets for Bitcoin.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import base58
import httpx

from app.config import get_settings
from app.services.connectors.base import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    Asset,
    BlockchainConnector,
    ChainTransaction,
    ConnectorError,
    TxIO,
)
from app.services.connectors.http import get_json

logger = logging.getLogger(__name__)
settings = get_settings()

# Etherscan and TronGrid answer in well under a second and an Esplora page in
# ~2s. A request still waiting at 10s is stuck, and every second it is allowed
# to wait lands on an officer watching "filing complaint" - the trace's time
# budget can decline to START new lookups but cannot interrupt one in flight.
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

#: Page budget per address lookup. A hot address (an exchange hot wallet) has
#: hundreds of thousands of transactions; walking all of them would hang the
#: trace and burn the rate limit for every other case being worked.
MAX_PAGES = 8


class _Pacer:
    """Spaces calls to at most `rate` per second, across every thread.

    The limit belongs to the API key, not to a connector instance or a request,
    so one pacer is shared by all of them. Holding the lock while sleeping is
    deliberate: it queues callers in arrival order instead of letting them all
    wake at once and burst straight back into the limit.
    """

    def __init__(self, rate: float):
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


#: Etherscan's free tier allows 3 calls per second per key (its NOTOK message
#: says so). Paced slightly under that, because the limit is enforced on
#: Etherscan's clock and ours drifts.
_ETHERSCAN_PACER = _Pacer(rate=2.8)

#: Extra attempts when Etherscan still reports the rate limit - possible if
#: another process shares the key.
RATE_LIMIT_RETRIES = 3


def _trongrid_answered(payload) -> bool:
    """TronGrid reports some failures as HTTP 200 with `success: false`."""
    return isinstance(payload, dict) and payload.get("success", True) is not False


def _etherscan_answered(payload) -> bool:
    """Cache only real answers: data, or a genuine "this address has none".

    Anything else from Etherscan (rate limit, invalid key, server busy) arrives
    as HTTP 200 too, and caching it served the refusal for five minutes.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("status") == "1":
        return True
    message = str(payload.get("message", ""))
    return "No transactions found" in message or "No records found" in message


def _tron_base58(hex_address: str) -> str | None:
    """Convert TronGrid's hex address form to the base58 form people read.

    The TRC-20 endpoint already returns base58, but the native-transaction
    endpoint returns 41-prefixed hex. Both have to come out in the same form or
    the same wallet becomes two nodes in the graph.

    Returns None rather than raising: one malformed address should cost that
    edge, not the whole trace.
    """
    if not hex_address:
        return None
    if hex_address.startswith("T"):  # already base58
        return hex_address
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return None
    if len(raw) != 21 or raw[0] != 0x41:
        return None
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    return base58.b58encode(raw + checksum).decode()


class EtherscanConnector(BlockchainConnector):
    """Ethereum via the Etherscan v2 API."""

    chain = "ETH"
    source_name = "etherscan"
    BASE_URL = "https://api.etherscan.io/v2/api"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.etherscan_api_key
        if not self.api_key:
            raise ConnectorError("ETHERSCAN_API_KEY is not configured")
        self._client = httpx.Client(timeout=_TIMEOUT)

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        """Native ETH, ERC-20 transfers and internal transfers, merged.

        All three are needed to follow stolen money on Ethereum:

        * `txlist` alone misses ERC-20 entirely, and fraud proceeds are
          overwhelmingly USDT/USDC - tracing only native ETH follows the gas,
          not the money.
        * `txlistinternal` carries value moved by contracts, which is where a
          trail through a DEX or a multisig continues.

        Each endpoint is capped at `limit`, so a wallet busy in one of them
        cannot crowd the others out of the result.
        """
        collected: list[ChainTransaction] = []
        native, signs_transactions = self._native(address, limit)
        collected.extend(native)
        collected.extend(self._tokens(address, limit))
        # Only a contract can be the SENDER of an internal transfer, and a
        # contract can never sign a transaction. So an address seen signing
        # one is an ordinary wallet, and every internal transfer involving it
        # is incoming - which forward tracing does not follow. Skipping the
        # call for those addresses is exact, not a heuristic, and it cuts an
        # Ethereum lookup from three rate-limited calls to two.
        if not signs_transactions:
            collected.extend(self._internal(address, limit))

        # Newest first, then trim: the caller asked for the most recent
        # `limit` movements of any kind, not `limit` of each.
        collected.sort(key=lambda t: t.timestamp, reverse=True)
        return collected[:limit]

    def _query(self, address: str, action: str, limit: int) -> list[dict]:
        params = {
            "chainid": 1,
            "module": "account",
            "action": action,
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": limit,
            "sort": "desc",
            "apikey": self.api_key,
        }
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            _ETHERSCAN_PACER.wait()
            payload = get_json(
                self._client,
                self.BASE_URL,
                params=params,
                source="etherscan",
                # A retry must reach Etherscan, not re-read the refusal.
                use_cache=attempt == 0,
                cache_if=_etherscan_answered,
            )
            if payload.get("status") == "1":
                result = payload.get("result", [])
                return result if isinstance(result, list) else []

            message = payload.get("message", "")
            detail = str(payload.get("result", ""))
            # Etherscan answers "No transactions found" with status "0" - not an error.
            if "No transactions found" in message or "No records found" in message:
                return []
            # The rate limit arrives as HTTP 200 + status "0" + "NOTOK", so the
            # HTTP layer's 429 handling never sees it. Without this branch a
            # throttled lookup became a ConnectorError, the address was dropped
            # from the trace, and the trace still reported itself complete -
            # an Ethereum wallet came back as 3 nodes instead of 42.
            if "rate limit" in detail.lower() and attempt < RATE_LIMIT_RETRIES:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise ConnectorError(f"etherscan error: {message} {detail}".strip())
        raise ConnectorError("etherscan error: rate limit retries exhausted")

    def _native(self, address: str, limit: int) -> tuple[list[ChainTransaction], bool]:
        """Native ETH transfers, plus whether `address` signed any transaction."""
        out: list[ChainTransaction] = []
        me = address.lower()
        signs = False
        for tx in self._query(address, "txlist", limit):
            if (tx.get("from") or "").lower() == me:
                signs = True
            if not tx.get("to"):  # contract creation - no recipient to trace
                continue
            value = Decimal(tx.get("value", "0")) / Decimal(10**18)
            # Etherscan reports a reverted transaction with isError="1" (and
            # txreceipt_status="0" post-Byzantium). It burned gas but moved no
            # value, so it must not become a transfer edge.
            failed = tx.get("isError") == "1" or tx.get("txreceipt_status") == "0"
            # A successful zero-value transaction is a contract CALL, not a
            # payment - an ERC-20 transfer shows up here as "0 ETH to the USDT
            # contract". The value it moved is already in `tokentx`. Keeping it
            # made the USDT contract itself (millions of transactions) the next
            # hop of the trace, spending budget on a path no money took.
            if value == 0 and not failed:
                continue
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    txid=tx["hash"],
                    timestamp=datetime.fromtimestamp(int(tx["timeStamp"]), tz=UTC),
                    block_height=int(tx.get("blockNumber", 0)) or None,
                    fee=Decimal(tx.get("gasUsed", "0")) * Decimal(tx.get("gasPrice", "0"))
                    / Decimal(10**18),
                    asset=Asset.native("ETH"),
                    status=STATUS_FAILED if failed else STATUS_SUCCESS,
                    inputs=[TxIO(address=tx["from"].lower(), value=value)],
                    outputs=[TxIO(address=tx["to"].lower(), value=value)],
                )
            )
        return out, signs

    def _tokens(self, address: str, limit: int) -> list[ChainTransaction]:
        """ERC-20 transfers - where fraud proceeds actually move on Ethereum."""
        out: list[ChainTransaction] = []
        for tx in self._query(address, "tokentx", limit):
            contract = (tx.get("contractAddress") or "").lower()
            if not contract or not tx.get("to") or not tx.get("from"):
                continue
            try:
                decimals = int(tx.get("tokenDecimal") or 18)
            except ValueError:
                decimals = 18
            value = Decimal(tx.get("value", "0")) / Decimal(10**decimals)
            # The contract is the token's identity: anyone can deploy a
            # contract calling itself "USDT", so the symbol alone is a claim,
            # not a fact.
            asset = Asset(
                chain=self.chain,
                symbol=tx.get("tokenSymbol") or "ERC20",
                contract=contract,
                decimals=decimals,
            )
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    # A single transaction can carry several token transfers,
                    # so the hash alone is not unique. The log index
                    # disambiguates them.
                    txid=f"{tx['hash']}#{tx.get('logIndex') or len(out)}",
                    timestamp=datetime.fromtimestamp(int(tx["timeStamp"]), tz=UTC),
                    block_height=int(tx.get("blockNumber", 0)) or None,
                    fee=Decimal(0),  # gas is charged once on the parent transaction
                    asset=asset,
                    status=STATUS_SUCCESS,  # a logged Transfer event means it succeeded
                    inputs=[TxIO(address=tx["from"].lower(), value=value)],
                    outputs=[TxIO(address=tx["to"].lower(), value=value)],
                )
            )
        return out

    def _internal(self, address: str, limit: int) -> list[ChainTransaction]:
        """Value moved by contracts - how a trail continues through a DEX."""
        out: list[ChainTransaction] = []
        for idx, tx in enumerate(self._query(address, "txlistinternal", limit)):
            if not tx.get("to") or not tx.get("from"):
                continue
            if tx.get("isError") == "1":
                continue
            value = Decimal(tx.get("value", "0")) / Decimal(10**18)
            if value == 0:  # a pure call with no value is not a transfer edge
                continue
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    # Internal transfers share the parent hash and Etherscan
                    # exposes no per-trace id, so position stands in for one.
                    txid=f"{tx['hash']}@{idx}",
                    timestamp=datetime.fromtimestamp(int(tx["timeStamp"]), tz=UTC),
                    block_height=int(tx.get("blockNumber", 0)) or None,
                    fee=Decimal(0),  # gas is charged once on the parent transaction
                    asset=Asset.native("ETH"),
                    status=STATUS_SUCCESS,
                    inputs=[TxIO(address=tx["from"].lower(), value=value)],
                    outputs=[TxIO(address=tx["to"].lower(), value=value)],
                )
            )
        return out

    def close(self) -> None:
        self._client.close()


#: Stand-in identity for a TRC-20 transfer TronGrid describes without a token.
#: Deliberately not a real contract address and deliberately not None: None
#: would make Asset report the transfer as native TRX and price it as such.
UNIDENTIFIED_TRC20_CONTRACT = "unidentified-trc20"
UNIDENTIFIED_TRC20_SYMBOL = "Unidentified TRC-20"


class TronGridConnector(BlockchainConnector):
    """TRON via TronGrid. Covers TRX and TRC-20 (USDT-TRC20 in particular)."""

    chain = "TRON"
    source_name = "trongrid"
    BASE_URL = "https://api.trongrid.io"
    USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.trongrid_api_key
        headers = {"TRON-PRO-API-KEY": self.api_key} if self.api_key else {}
        self._client = httpx.Client(timeout=_TIMEOUT, headers=headers)

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        """TRC-20 transfers and native TRX, merged, newest first.

        TRC-20 carries the USDT that most TRON fraud moves in, but a trail that
        converts to TRX - to pay for energy, or to move through a TRX-only
        service - disappears entirely if only TRC-20 is fetched.
        """
        collected = self._trc20(address, limit) + self._native(address, limit)
        collected.sort(key=lambda t: t.timestamp, reverse=True)
        return collected[:limit]

    def _trc20(self, address: str, limit: int) -> list[ChainTransaction]:
        # Both TronGrid account endpoints return newest-first already. Do NOT
        # add `order_by=block_timestamp,desc` to make that explicit: TronGrid
        # answers that parameter with an empty result set, so the "fix" would
        # silently return no transactions at all.
        url = f"{self.BASE_URL}/v1/accounts/{address}/transactions/trc20"
        payload = get_json(
            self._client, url, params={"limit": min(limit, 200)}, source="trongrid",
            cache_if=_trongrid_answered,
        )

        if not payload.get("success", True):
            raise ConnectorError(f"trongrid error: {payload.get('error', payload)}")

        out: list[ChainTransaction] = []
        for tx in payload.get("data", []):
            info = tx.get("token_info", {}) or {}
            contract = info.get("address")
            if contract:
                # `token_info.address` is the TRC-20 contract - the token's
                # actual identity. Without it "USDT" is just a name anyone can
                # claim.
                decimals = int(info.get("decimals", 6))
                symbol = info.get("symbol", "TRC20")
                value = Decimal(str(tx.get("value", "0"))) / Decimal(10**decimals)
            else:
                # TronGrid returns "token_info": {} for a fraction of transfers
                # (5 of 25 on one live account). Filling the gap with defaults
                # is not harmless: contract=None is how Asset spells "native
                # TRX", so the transfer would be priced at the TRX rate, and a
                # guessed decimals rescales the amount by an arbitrary power of
                # ten. Together those turned one real transfer into a 1.0e26
                # rupee exposure. The movement itself is still evidence, so the
                # edge is kept - as an asset that cannot be named, scaled or
                # priced.
                decimals = 0
                symbol = UNIDENTIFIED_TRC20_SYMBOL
                contract = UNIDENTIFIED_TRC20_CONTRACT
                value = Decimal(str(tx.get("value", "0")))
            asset = Asset(
                chain=self.chain,
                symbol=symbol,
                contract=contract,
                decimals=decimals,
            )
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    txid=tx["transaction_id"],
                    timestamp=datetime.fromtimestamp(
                        int(tx["block_timestamp"]) / 1000, tz=UTC
                    ),
                    asset=asset,
                    inputs=[TxIO(address=tx["from"], value=value)],
                    outputs=[TxIO(address=tx["to"], value=value)],
                )
            )
        return out

    def _native(self, address: str, limit: int) -> list[ChainTransaction]:
        """Native TRX transfers (TransferContract)."""
        url = f"{self.BASE_URL}/v1/accounts/{address}/transactions"
        payload = get_json(
            self._client,
            url,
            params={"limit": min(limit, 200), "only_confirmed": "true"},
            source="trongrid",
            cache_if=_trongrid_answered,
        )
        if not payload.get("success", True):
            raise ConnectorError(f"trongrid error: {payload.get('error', payload)}")

        out: list[ChainTransaction] = []
        for tx in payload.get("data", []):
            try:
                contract = (tx["raw_data"]["contract"] or [])[0]
            except (KeyError, IndexError, TypeError):
                continue
            # Only plain TRX transfers carry a value edge. Smart-contract calls
            # arrive as TriggerSmartContract and are already covered by the
            # TRC-20 endpoint, so including them here would double-count.
            if contract.get("type") != "TransferContract":
                continue
            params = (contract.get("parameter") or {}).get("value") or {}
            owner, to = params.get("owner_address"), params.get("to_address")
            if not owner or not to:
                continue

            # TronGrid returns hex addresses here; the rest of the system works
            # in base58, which is also what an officer reads on an explorer.
            sender, recipient = _tron_base58(owner), _tron_base58(to)
            if not sender or not recipient:
                continue

            value = Decimal(str(params.get("amount", 0))) / Decimal(10**6)
            # contractRet is "SUCCESS" only when the transfer actually landed.
            ret = ((tx.get("ret") or [{}])[0]).get("contractRet", "")
            out.append(
                ChainTransaction(
                    chain=self.chain,
                    txid=tx["txID"],
                    timestamp=datetime.fromtimestamp(int(tx["block_timestamp"]) / 1000, tz=UTC),
                    block_height=tx.get("blockNumber"),
                    asset=Asset.native("TRX"),
                    status=STATUS_SUCCESS if ret == "SUCCESS" else STATUS_FAILED,
                    inputs=[TxIO(address=sender, value=value)],
                    outputs=[TxIO(address=recipient, value=value)],
                )
            )
        return out

    def close(self) -> None:
        self._client.close()


class BlockchairConnector(BlockchainConnector):
    """Bitcoin via Blockchair. Works keyless at low rates; a key raises limits.

    Returns genuine UTXO input/output sets, which is what the clustering
    heuristics need.
    """

    chain = "BTC"
    source_name = "blockchair"
    BASE_URL = "https://api.blockchair.com/bitcoin"
    SATS = Decimal(10**8)

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.blockchair_api_key
        self._client = httpx.Client(timeout=_TIMEOUT)

    def _params(self, **extra) -> dict:
        params = dict(extra)
        if self.api_key:
            params["key"] = self.api_key
        return params

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        payload = get_json(
            self._client,
            f"{self.BASE_URL}/dashboards/address/{address}",
            params=self._params(limit=min(limit, 100)),
            source="blockchair",
        )
        addr_data = (payload.get("data") or {}).get(address, {})

        txids = (addr_data or {}).get("transactions", [])[:limit]
        if not txids:
            return []

        # Blockchair caps the multi-transaction dashboard at 10 hashes per call.
        out: list[ChainTransaction] = []
        for batch_start in range(0, len(txids), 10):
            batch = txids[batch_start : batch_start + 10]
            batch_payload = get_json(
                self._client,
                f"{self.BASE_URL}/dashboards/transactions/{','.join(batch)}",
                params=self._params(),
                source="blockchair",
            )
            data = batch_payload.get("data") or {}

            for txid, entry in data.items():
                tx = entry.get("transaction", {})
                out.append(
                    ChainTransaction(
                        chain=self.chain,
                        txid=txid,
                        timestamp=datetime.fromisoformat(tx["time"]).replace(tzinfo=UTC),
                        block_height=tx.get("block_id"),
                        fee=Decimal(str(tx.get("fee", 0))) / self.SATS,
                        asset=Asset.native("BTC"),
                        inputs=[
                            TxIO(
                                address=i.get("recipient", ""),
                                value=Decimal(str(i.get("value", 0))) / self.SATS,
                                index=idx,
                            )
                            for idx, i in enumerate(entry.get("inputs", []))
                            if i.get("recipient")
                        ],
                        outputs=[
                            TxIO(
                                address=o.get("recipient", ""),
                                value=Decimal(str(o.get("value", 0))) / self.SATS,
                                index=idx,
                            )
                            for idx, o in enumerate(entry.get("outputs", []))
                            if o.get("recipient")
                        ],
                    )
                )
        out.sort(key=lambda t: t.timestamp, reverse=True)
        return out

    def close(self) -> None:
        self._client.close()


class EsploraConnector(BlockchainConnector):
    """Bitcoin via Blockstream's Esplora API.

    The default BTC source because it needs no API key. Blockchair's free tier
    is too small to trace with and its paid key is not budgeted, which had left
    Bitcoin with no live source at all. Esplora serves the same data a block
    explorer shows, from Blockstream's public index.

    Returns genuine UTXO input/output sets, so the common-input-ownership and
    change-address heuristics work exactly as they do on Blockchair.
    """

    chain = "BTC"
    source_name = "esplora"
    BASE_URL = "https://blockstream.info/api"
    SATS = Decimal(10**8)

    #: Esplora returns 25 confirmed transactions per page and offers no way to
    #: ask for a larger page, so depth comes from paging rather than a limit.
    PAGE_SIZE = 25

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.esplora_base_url).rstrip("/")
        self._client = httpx.Client(timeout=_TIMEOUT)

    def get_transactions(self, address: str, limit: int = 50) -> list[ChainTransaction]:
        out: list[ChainTransaction] = []
        last_seen: str | None = None

        for _ in range(MAX_PAGES):
            if len(out) >= limit:
                break
            url = f"{self.base_url}/address/{address}/txs"
            if last_seen:
                url = f"{url}/chain/{last_seen}"

            page = get_json(self._client, url, source=self.source_name)
            if not isinstance(page, list) or not page:
                break

            out.extend(self._to_transaction(tx) for tx in page)
            last_seen = page[-1].get("txid")
            if len(page) < self.PAGE_SIZE or not last_seen:
                break

        out.sort(key=lambda t: t.timestamp, reverse=True)
        return out[:limit]

    def _to_transaction(self, tx: dict) -> ChainTransaction:
        status = tx.get("status") or {}
        # An unconfirmed transaction has no block time. It is still real and
        # still spendable, so it is kept and stamped with now rather than
        # dropped: money sitting in the mempool is exactly what an officer
        # chasing a live theft needs to see.
        block_time = status.get("block_time")
        timestamp = (
            datetime.fromtimestamp(int(block_time), tz=UTC) if block_time else datetime.now(UTC)
        )

        inputs = []
        for idx, vin in enumerate(tx.get("vin") or []):
            prevout = vin.get("prevout") or {}
            address = prevout.get("scriptpubkey_address")
            if not address:  # coinbase, or a script with no address form
                continue
            inputs.append(
                TxIO(
                    address=address,
                    value=Decimal(str(prevout.get("value", 0))) / self.SATS,
                    index=idx,
                )
            )

        outputs = []
        for idx, vout in enumerate(tx.get("vout") or []):
            address = vout.get("scriptpubkey_address")
            if not address:  # OP_RETURN and other non-address outputs
                continue
            outputs.append(
                TxIO(
                    address=address,
                    value=Decimal(str(vout.get("value", 0))) / self.SATS,
                    index=idx,
                )
            )

        return ChainTransaction(
            chain=self.chain,
            txid=tx["txid"],
            timestamp=timestamp,
            block_height=status.get("block_height"),
            fee=Decimal(str(tx.get("fee", 0))) / self.SATS,
            asset=Asset.native("BTC"),
            status=STATUS_SUCCESS,
            inputs=inputs,
            outputs=outputs,
        )

    def close(self) -> None:
        self._client.close()
