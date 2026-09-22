"""Prove each live connector really reaches its chain and parses what it gets.

Run before trusting DEMO_MODE=false. Uses well-known public addresses, so a
failure here is a connector or key problem, never an empty-wallet problem:

  BTC   Bitcoin's first-ever recipient block reward address (Satoshi/Hal era)
  ETH   a Binance hot wallet - constant native, ERC-20 and internal activity
  TRON  a Binance TRON hot wallet - constant TRC-20 (USDT) and TRX activity

Usage:  python scripts/check_live.py
"""

from __future__ import annotations

import sys
from collections import Counter

PROBES = {
    "BTC": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "ETH": "0x28C6c06298d514Db089934071355E5743bf21d60",
    "TRON": "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9",
}


def main() -> int:
    from app.services.connectors import get_connector
    from app.services.connectors.base import ConnectorError

    failures = 0
    for chain, address in PROBES.items():
        print(f"\n=== {chain} · {address} ===", flush=True)
        try:
            # demo_mode=False forces the live connector regardless of .env, so
            # this script tests the real path even while the stack is still in
            # demo mode.
            connector = get_connector(chain, demo_mode=False)
        except ConnectorError as exc:
            print(f"  FAIL  no connector: {exc}")
            failures += 1
            continue

        source = getattr(connector, "source_name", "?")
        if source == "synthetic":
            print("  FAIL  fell back to SYNTHETIC - the live source is not configured")
            failures += 1
            connector.close()
            continue

        try:
            txs = connector.get_transactions(address, limit=25)
        except Exception as exc:  # noqa: BLE001 - report, don't abort the other chains
            print(f"  FAIL  {source}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        finally:
            connector.close()

        if not txs:
            print(f"  FAIL  {source} returned no transactions for a known-active address")
            failures += 1
            continue

        assets = Counter(t.asset.symbol for t in txs)
        newest = max(t.timestamp for t in txs)
        with_io = sum(1 for t in txs if t.inputs and t.outputs)
        print(f"  source        : {source}")
        print(f"  transactions  : {len(txs)}")
        print(f"  assets        : {dict(assets)}")
        print(f"  newest        : {newest.isoformat()}")
        print(f"  with in+out   : {with_io}/{len(txs)}")
        sample = txs[0]
        print(f"  sample txid   : {sample.txid}")
        print(
            f"  sample edge   : {sample.inputs[0].address[:16]}… -> "
            f"{sample.outputs[0].address[:16]}… "
            f"{sample.outputs[0].value} {sample.asset.symbol}"
        )

        # A transaction with no input or no output is not a traceable edge.
        if with_io == 0:
            print("  FAIL  no transaction carried both a sender and a recipient")
            failures += 1

    print("\n" + "=" * 60)
    if failures:
        print(f"{failures} chain(s) FAILED - do not set DEMO_MODE=false yet")
        return 1
    print("All live connectors reached their chain and parsed real transactions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
