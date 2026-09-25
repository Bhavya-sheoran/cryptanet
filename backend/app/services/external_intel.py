"""Third-party intelligence: Arkham entity labels and Chainabuse scam reports.

Two different questions, kept apart on purpose:

  * **Arkham - who controls this wallet?** Attribution for addresses the curated
    tag database does not know. An Arkham label is recorded as a tag with
    source="arkham", so the next trace through that address finds it locally,
    and every screen that shows it can say where it came from. It ranks below
    the curated sources (OFAC, Binance's own reserve list, Etherscan labels) and
    above the behavioural classifier - which never names an entity at all.

  * **Chainabuse - has anyone reported this wallet as a scam?** Corroborating
    evidence about the suspect wallet itself. It is shown and recorded in the
    case file, but it does NOT feed the exchange fraud-linkage score: that score
    is about where victims' money lands, and a scam report is about the wallet
    that took it. Mixing the two would double-count the same complaint.

Both need an API key and are off without one. Off means "not checked" - never
"no reports" and never "not identified", because an absent key is not evidence.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime

import httpx

from app.config import get_settings
from app.db import redis_client

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_CACHE_PREFIX = "sih183:intel:"

SOURCE_ARKHAM = "arkham"

#: Our chain -> Arkham's chain key in the /all response.
ARKHAM_CHAINS = {"BTC": "bitcoin", "ETH": "ethereum", "TRON": "tron"}

#: Arkham entity types -> this system's entity_type. Conservative on purpose:
#: only custodial services map to "exchange", because that is the label that
#: tells an officer "a legal request here can identify the account holder". A
#: decentralised exchange cannot answer one, so it stays "unknown" (the name is
#: still shown).
ARKHAM_TYPES = {
    "cex": "exchange",
    "exchange": "exchange",
    "mixer": "mixer",
    "gambling": "gambling",
    "casino": "gambling",
    "payments": "payment_processor",
    "payment-processor": "payment_processor",
    "sanctioned": "sanctioned",
    "darknet": "darknet",
    "darknet-market": "darknet",
}

#: How long a lookup that found nothing is remembered. A found label is stored
#: permanently as a tag; a miss is re-asked after this, since Arkham's coverage
#: grows.
NEGATIVE_TTL_SECONDS = 7 * 86400
CHAINABUSE_TTL_SECONDS = 6 * 3600


# ---------------------------------------------------------------------------
# Small cache helpers
# ---------------------------------------------------------------------------
def _cache_get(key: str):
    try:
        raw = redis_client.get_client().get(_CACHE_PREFIX + key)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 - a cache outage must not break a lookup
        return None


def _cache_put(key: str, value, ttl: int) -> None:
    try:
        redis_client.get_client().setex(_CACHE_PREFIX + key, ttl, json.dumps(value))
    except Exception:  # noqa: BLE001
        logger.debug("intel cache write failed; continuing without cache")


# ---------------------------------------------------------------------------
# Arkham
# ---------------------------------------------------------------------------
def arkham_enabled() -> bool:
    return bool(get_settings().arkham_api_key)


def parse_arkham(payload, chain: str) -> dict | None:
    """Pull the entity out of an /intelligence/address/{a}/all response.

    The response maps Arkham chain names to address objects; an attributed one
    carries `arkhamEntity`. The object for our chain is preferred, but an
    entity found on any chain for the same address string is the same entity.
    Returns None when Arkham has no entity - a bare `arkhamLabel` without an
    entity is a description, not an attribution, and is not treated as one.
    """
    if not isinstance(payload, dict):
        return None
    objects = []
    preferred = payload.get(ARKHAM_CHAINS.get(chain, ""))
    if isinstance(preferred, dict):
        objects.append(preferred)
    objects += [v for k, v in payload.items() if isinstance(v, dict) and v is not preferred]
    # The single-chain form of the endpoint returns the address object itself.
    if "arkhamEntity" in payload:
        objects.insert(0, payload)

    for obj in objects:
        entity = obj.get("arkhamEntity")
        if isinstance(entity, dict) and entity.get("name"):
            raw_type = str(entity.get("type") or "").lower()
            label = obj.get("arkhamLabel") or {}
            return {
                "entity_name": str(entity["name"]).strip(),
                "entity_type": ARKHAM_TYPES.get(raw_type, "unknown"),
                "arkham_type": raw_type or None,
                "arkham_id": entity.get("id"),
                "label": (label.get("name") if isinstance(label, dict) else None)
                or str(entity["name"]),
            }
    return None


def arkham_lookup(chain: str, address_norm: str) -> dict | None:
    """Arkham's entity for an address, or None. Never raises."""
    settings = get_settings()
    if not settings.arkham_api_key or chain not in ARKHAM_CHAINS:
        return None

    key = f"arkham:{chain}:{address_norm}"
    cached = _cache_get(key)
    if cached is not None:
        return cached or None  # {} marks a remembered miss

    try:
        resp = httpx.get(
            f"{settings.arkham_base_url.rstrip('/')}/intelligence/address/{address_norm}/all",
            headers={"API-Key": settings.arkham_api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            _cache_put(key, {}, NEGATIVE_TTL_SECONDS)
            return None
        resp.raise_for_status()
        found = parse_arkham(resp.json(), chain)
    except (httpx.HTTPError, ValueError) as exc:
        # Not cached: a failure is not an answer, and the next analysis retries.
        logger.warning("Arkham lookup failed for %s:%s: %s", chain, address_norm, exc)
        return None

    if found is None:
        _cache_put(key, {}, NEGATIVE_TTL_SECONDS)
        return None
    persist_tag(chain, address_norm, found)
    return found


def persist_tag(chain: str, address_norm: str, found: dict) -> None:
    """Record an Arkham label as a tag (Postgres + Neo4j), like any seeded tag.

    Stored so the next trace meets it locally - terminal detection, stopping at
    services and attribution all read tags from the graph - and so the label is
    never fetched twice.
    """
    from sqlalchemy import select

    from app.db.neo4j import get_driver
    from app.db.postgres import SessionLocal
    from app.models import Entity, TaggedAddress

    try:
        with SessionLocal() as db:
            entity = db.execute(
                select(Entity).where(
                    Entity.name == found["entity_name"],
                    Entity.entity_type == found["entity_type"],
                )
            ).scalar_one_or_none()
            if entity is None:
                entity = Entity(name=found["entity_name"], entity_type=found["entity_type"])
                db.add(entity)
                db.flush()
            exists = db.execute(
                select(TaggedAddress).where(
                    TaggedAddress.chain == chain,
                    TaggedAddress.address_norm == address_norm,
                    TaggedAddress.source == SOURCE_ARKHAM,
                )
            ).scalar_one_or_none()
            if exists is None:
                db.add(
                    TaggedAddress(
                        address_norm=address_norm,
                        chain=chain,
                        entity_id=entity.id,
                        label=(found.get("label") or found["entity_name"])[:500],
                        source=SOURCE_ARKHAM,
                        confidence=0.85,
                    )
                )
            db.commit()
            entity_id = str(entity.id)

        with get_driver().session() as session:
            session.run(
                """
                MERGE (e:Entity {entity_id: $entity_id})
                  ON CREATE SET e.name = $name, e.entity_type = $entity_type
                MERGE (a:Address {chain: $chain, address_norm: $address_norm})
                  ON CREATE SET a.address = $address_norm, a.tx_count = 0
                SET a.entity_type = $entity_type,
                    a.is_mixer = ($entity_type = 'mixer')
                MERGE (a)-[t:TAGGED_AS {source: $source}]->(e)
                  ON CREATE SET t.confidence = 0.85
                """,
                entity_id=entity_id,
                name=found["entity_name"],
                entity_type=found["entity_type"],
                chain=chain,
                address_norm=address_norm,
                source=SOURCE_ARKHAM,
            ).consume()
    except Exception:  # noqa: BLE001 - failing to store must not lose the answer
        logger.warning("could not store Arkham tag for %s:%s", chain, address_norm, exc_info=True)


def enrich_with_arkham(chain: str, addresses: list[str]) -> int:
    """Ask Arkham about unlabelled addresses where a trace ended. Returns hits.

    Bounded twice - by count and by wall-clock - because this runs inside
    complaint intake, which the officer is waiting on. Lookups still running
    when the window closes finish in the background and are stored for the
    next analysis.
    """
    settings = get_settings()
    if not settings.arkham_api_key or not addresses:
        return 0
    batch = addresses[: settings.arkham_max_lookups]
    pool = ThreadPoolExecutor(max_workers=min(4, len(batch)))
    futures = [pool.submit(arkham_lookup, chain, a) for a in batch]
    done, _pending = wait(futures, timeout=settings.arkham_time_budget_seconds)
    pool.shutdown(wait=False)
    return sum(1 for f in done if f.exception() is None and f.result())


# ---------------------------------------------------------------------------
# Chainabuse
# ---------------------------------------------------------------------------
def chainabuse_enabled() -> bool:
    return bool(get_settings().chainabuse_api_key)


def _reports_of(payload) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("reports", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def summarise_chainabuse(payload, address: str) -> dict:
    """Condense a /reports response into what a case file needs."""
    reports = _reports_of(payload)
    categories: dict[str, int] = {}
    latest = None
    for r in reports:
        cat = r.get("scamCategory") or r.get("category") or "unspecified"
        categories[str(cat)] = categories.get(str(cat), 0) + 1
        created = r.get("createdAt") or r.get("created_at")
        if created and (latest is None or str(created) > latest):
            latest = str(created)
    total = payload.get("count") if isinstance(payload, dict) else None
    return {
        "status": "checked",
        "report_count": int(total) if isinstance(total, int) else len(reports),
        "trusted_reports": sum(1 for r in reports if r.get("trusted")),
        "verified_reports": sum(1 for r in reports if r.get("checked")),
        "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "latest_report_at": latest,
        "source": "chainabuse",
        "url": f"https://www.chainabuse.com/address/{address}",
        "checked_at": datetime.now(UTC).isoformat(),
    }


def chainabuse_reports(chain: str, address: str) -> dict:
    """Scam reports filed against `address` on Chainabuse.

    Always returns a dict with a `status` a UI can show plainly:
    `not_configured` (no key), `unavailable` (lookup failed) or `checked`.
    """
    settings = get_settings()
    if not settings.chainabuse_api_key:
        return {"status": "not_configured", "source": "chainabuse"}

    key = f"chainabuse:{chain}:{address}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        resp = httpx.get(
            f"{settings.chainabuse_base_url.rstrip('/')}/reports",
            params={"address": address, "perPage": 50},
            # Chainabuse: Basic auth, the API key as the username.
            auth=(settings.chainabuse_api_key, settings.chainabuse_api_key),
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        summary = summarise_chainabuse(resp.json(), address)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Chainabuse lookup failed for %s: %s", address, exc)
        return {"status": "unavailable", "source": "chainabuse"}

    _cache_put(key, summary, CHAINABUSE_TTL_SECONDS)
    return summary
