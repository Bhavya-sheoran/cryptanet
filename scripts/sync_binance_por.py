#!/usr/bin/env python3
"""Pull Binance's Proof-of-Reserves wallet list and load it as exchange tags.

Binance publishes every wallet it counts towards its reserves, on its Proof of
Reserves page (binance.com/en/proof-of-reserves). That makes these the most
reliable exchange labels this system has: the exchange itself asserts
ownership, publicly, for the purpose of being audited. Money that reaches one
of these addresses reached Binance, and Binance is where a legal request can
identify the account holder.

The seeded GraphSense pack held 86 of these addresses as of November 2022; the
live list is longer and Binance keeps adding to it, so this is re-runnable.

Writes ml/seeds/binance_por.json (so a full re-seed from disk includes it, and
the list used is on record) and loads the tags through the normal seeder.

Usage:
    docker compose exec backend python scripts/sync_binance_por.py
    docker compose exec backend python scripts/sync_binance_por.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

POR_URL = "https://www.binance.com/bapi/apex/v1/public/apex/market/por/address"

#: Binance network code -> chain this system can trace. Everything else in the
#: list (BSC, SOL, XRP...) is a chain there is no connector for, and a tag we
#: can never meet on a trace is noise.
NETWORKS = {"BTC": "BTC", "ETH": "ETH", "TRX": "TRON"}

SOURCE = "binance_por"
SEED_FILE = "binance_por.json"


def fetch() -> list[dict]:
    resp = httpx.get(POR_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "000000" or not isinstance(body.get("data"), list):
        raise SystemExit(f"unexpected Proof-of-Reserves response: {str(body)[:200]}")
    return body["data"]


def to_tags(records: list[dict]) -> list[dict]:
    tags = []
    for r in records:
        chain = NETWORKS.get(str(r.get("network", "")).upper())
        address = str(r.get("address") or "").strip()
        if chain is None or not address:
            continue
        custodian = (r.get("thirdPartyCustodianName") or "").strip()
        label = "Binance reserve wallet (Proof of Reserves)"
        if custodian:
            # Held for Binance by a third-party custodian. Still Binance's
            # reserves, but the wallet operator is someone else - which matters
            # to whoever receives the legal request.
            label = f"Binance reserve wallet, held by custodian {custodian} (Proof of Reserves)"
        tags.append(
            {
                "chain": chain,
                "address_norm": address.lower() if chain == "ETH" else address,
                "entity_name": "Binance",
                "entity_type": "exchange",
                "label": label,
                "source": SOURCE,
                "confidence": 1.0,
            }
        )
    # One tag per (chain, address): the list repeats an address per asset.
    unique = {(t["chain"], t["address_norm"]): t for t in tags}
    return list(unique.values())


def seeds_dir() -> Path:
    for p in (Path("/app/ml_seeds"), Path(__file__).resolve().parents[1] / "ml" / "seeds"):
        if p.exists():
            return p
    raise SystemExit("no seeds directory found")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = ap.parse_args()

    records = fetch()
    tags = to_tags(records)
    print(f"Binance Proof of Reserves: {len(records)} published addresses")
    print(f"  on traceable chains: {len(tags)}  {dict(Counter(t['chain'] for t in tags))}")
    held = sum(1 for t in tags if "custodian" in t["label"])
    print(f"  held by a third-party custodian: {held}")
    if args.dry_run:
        return 0

    path = seeds_dir() / SEED_FILE
    path.write_text(
        json.dumps(
            {
                "source_url": POR_URL,
                "fetched_at": datetime.now(UTC).isoformat(),
                "tags": tags,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"  written to {path}")

    sys.path.insert(0, str(Path("/app/ml/src")))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ml" / "src"))
    from seed_tags import load

    stats = load(tags)
    print(f"  loaded: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
