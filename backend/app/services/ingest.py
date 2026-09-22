"""Wallet intake orchestration.

One complaint arrives, and this walks it end to end:

    validate address -> resolve/create wallet -> dedupe against prior cases
    -> create case -> BFS expand the money flow via a connector
    -> write the graph -> run clustering -> report what happened

Kept out of the API layer so Phase 4's NCRP intake endpoint and the synthetic
demo loader can reuse exactly the same path a manual submission takes.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import metrics
from app.config import get_settings
from app.models import Case, CaseWallet, TraceRun, Wallet
from app.services import clustering, graph_writer, illicit_model
from app.services.chain_detect import AddressInfo, detect, normalize_address
from app.services.connectors import get_connector
from app.services.connectors.base import BlockchainConnector, ChainTransaction, ConnectorError

logger = logging.getLogger(__name__)
settings = get_settings()

# Known mixer addresses are tagged in Phase 2; until then the graph flag set by
# the tagged-address seed is the signal. Interaction is recorded, never unwound.
MIXER_FLAG_PROPERTY = "is_mixer"


class IntakeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Wallet + case persistence
# ---------------------------------------------------------------------------
def resolve_or_create_wallet(db: Session, info: AddressInfo) -> tuple[Wallet, bool]:
    """Fetch the wallet row for this address, creating it if new."""
    existing = db.execute(
        select(Wallet).where(
            Wallet.chain == info.chain, Wallet.address_norm == info.address_norm
        )
    ).scalar_one_or_none()

    if existing is not None:
        return existing, False

    wallet = Wallet(
        address=info.address.strip(),
        address_norm=info.address_norm,
        chain=info.chain,
        address_kind=info.address_kind,
    )
    db.add(wallet)
    db.flush()
    return wallet, True


def find_prior_cases(db: Session, wallet: Wallet) -> list[Case]:
    """Cases that already implicate this wallet - the multi-victim signal."""
    return list(
        db.execute(
            select(Case)
            .join(CaseWallet, CaseWallet.case_id == Case.id)
            .where(CaseWallet.wallet_id == wallet.id)
            .order_by(Case.reported_at.desc())
        )
        .scalars()
        .all()
    )


def next_case_number(db: Session) -> str:
    """Sequential, human-quotable case reference."""
    year = datetime.now(UTC).year
    prefix = f"SIH183-{year}-"
    count = db.execute(
        select(func.count()).select_from(Case).where(Case.case_number.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}{count + 1:06d}"


# ---------------------------------------------------------------------------
# Graph expansion
# ---------------------------------------------------------------------------
#: Entity types whose addresses end a trace. Custodial services only - the
#: money is deposited and mixes with every other customer's. Mixers are NOT
#: here: funds genuinely pass through a mixer and continue, and the system's
#: stance is to flag mixer contact, not to stop following the money there.
CUSTODIAL_ENTITY_TYPES = ("exchange", "payment_processor", "gambling")

_CUSTODIAL = """
MATCH (a:Address {chain: $chain})-[:TAGGED_AS]->(e:Entity)
WHERE a.address_norm IN $addresses AND e.entity_type IN $types
RETURN DISTINCT a.address_norm AS address
"""


def _custodial_addresses(chain: str, addresses: list[str]) -> set[str]:
    """Which of `addresses` belong to a tagged custodial service.

    One query per trace level, against the seeded tag database. A tag lookup
    failure returns an empty set: the trace then simply expands as it did
    before this optimisation existed, which is slower but never wrong.
    """
    if not addresses:
        return set()
    from app.db.neo4j import get_driver

    try:
        with get_driver().session() as session:
            return {
                r["address"]
                for r in session.run(
                    _CUSTODIAL,
                    chain=chain,
                    addresses=list(addresses),
                    types=list(CUSTODIAL_ENTITY_TYPES),
                )
            }
    except Exception:  # noqa: BLE001 - see docstring: degrade to plain expansion
        logger.warning("custodial tag lookup failed; expanding without it", exc_info=True)
        return set()


#: Returned for an address whose lookup never started because the trace's
#: time budget ran out first.
_SKIPPED: list = []


def _fetch_frontier(
    connector: BlockchainConnector,
    addresses: list[str],
    limit: int,
    depth: int,
    deadline: float | None = None,
) -> list[tuple[str, list[ChainTransaction] | None]]:
    """Fetch every address at one depth level, concurrently.

    Pure network I/O against a public indexer, so threads apply cleanly and
    httpx.Client is documented as thread-safe. Results come back as
    (address, transactions) pairs and are processed by the caller on one
    thread, which keeps `seen`, `collected` and the next frontier free of any
    concurrent mutation.

    An address whose lookup fails yields None rather than aborting the level:
    one indexer error should cost that branch, not the whole trace. None and
    not an empty list, because "this wallet has no transactions" and "we could
    not ask" are different findings, and the caller must count the second.
    """
    if len(addresses) == 1:
        # Not worth a pool, and keeps single-address traces easy to follow in
        # a stack trace.
        return [(addresses[0], _safe_fetch(connector, addresses[0], limit, depth, deadline))]

    workers = min(get_settings().trace_concurrency, len(addresses))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            (address, pool.submit(_safe_fetch, connector, address, limit, depth, deadline))
            for address in addresses
        ]
        # Returned in the order the addresses were given, not the order the
        # lookups happened to finish. The caller builds the next frontier from
        # this, and the call budget then decides which of those addresses are
        # expanded - so completion order would make the same complaint trace
        # to a different graph on each run. A trace has to be reproducible to
        # be evidence.
        return [(address, future.result()) for address, future in futures]


def _safe_fetch(
    connector: BlockchainConnector,
    address: str,
    limit: int,
    depth: int,
    deadline: float | None = None,
) -> list[ChainTransaction] | None:
    # Checked when the lookup actually starts, not when it was queued: behind a
    # rate-limited provider, work queued early can still be waiting when the
    # budget ends, and starting it then is exactly the overrun being prevented.
    if deadline is not None and time.monotonic() >= deadline:
        return _SKIPPED
    try:
        metrics.upstream_calls_total.labels(source=connector.source_name).inc()
        return connector.get_transactions(address, limit=limit)
    except ConnectorError as exc:
        # Counted, not just logged. An indexer failing does not break anything
        # visibly - it silently shortens every trace, and the truncated answer
        # still looks like an answer.
        metrics.indexer_errors_total.labels(source=connector.source_name).inc()
        logger.warning(
            "connector failed for %s at depth %d: %s",
            address,
            depth,
            exc,
            extra={"source": connector.source_name, "depth": depth},
        )
        return None


def expand_money_flow(
    connector: BlockchainConnector,
    chain: str,
    root_address: str,
    max_depth: int,
    max_breadth: int,
) -> dict:
    """Breadth-first walk of the outbound money flow from `root_address`.

    Follows the direction the funds moved: from an address we take the
    transactions it *spent* into, and the outputs of those become the next hop.
    Transactions where the address only received are still written to the graph
    for context (they show where the victim's money came in) but are not
    expanded, or every trace would balloon backwards into unrelated history.
    """
    seen: set[str] = set()
    frontier = [root_address]
    collected: dict[str, ChainTransaction] = {}
    hops = 0

    # One upstream call per address expanded, and nothing previously bounded
    # how many addresses reached the next level: `max_breadth` caps
    # transactions per address, not the fan-out those transactions produce. A
    # wallet that spent into 25 transactions with 100 outputs each yields 2,500
    # addresses at depth 1, and squares from there. On real data the walk stays
    # small, but "small in the cases we tried" is not a bound - and the person
    # who exhausts a 100,000-call daily quota does it with one click, having
    # been given no indication that the click was expensive.
    budget = max(1, settings.connector_call_budget)
    calls_made = 0
    budget_exhausted = False
    frontier_truncated = False
    lookup_failures = 0
    stopped_at_services = 0
    deadline = time.monotonic() + settings.trace_time_budget_seconds

    for depth in range(max_depth):
        if not frontier or budget_exhausted:
            break
        if time.monotonic() >= deadline:
            budget_exhausted = True
            logger.warning(
                "trace of %s stopped at depth %d: %.0fs time budget spent",
                root_address,
                depth,
                settings.trace_time_budget_seconds,
            )
            break
        next_frontier: list[str] = []

        # Custodial services end the trail. The edge INTO the exchange is
        # already recorded (it came from the previous hop's transactions), so
        # attribution, exposure and risk all still see it - what is skipped is
        # walking the exchange's own hot wallet, which is thousands of other
        # customers' money and says nothing about this victim's. It was also
        # where most of the call budget went. Never applied to the reported
        # address itself: a complaint can legitimately name an exchange wallet.
        if depth > 0 and settings.trace_stop_at_services:
            endpoints = _custodial_addresses(chain, [a for a in frontier if a not in seen])
            if endpoints:
                seen.update(endpoints)
                stopped_at_services += len(endpoints)
                frontier = [a for a in frontier if a not in endpoints]

        # Decide this level's batch up front, so the budget is still spent
        # exactly and `seen` is updated on one thread before any fetching
        # starts. Everything after this point is read-only on that set.
        batch: list[str] = []
        for address in frontier:
            if address in seen:
                continue
            if calls_made + len(batch) >= budget:
                budget_exhausted = True
                logger.warning(
                    "trace of %s stopped at depth %d: upstream call budget of %d spent",
                    root_address,
                    depth,
                    budget,
                )
                break
            batch.append(address)

        if not batch:
            break

        seen.update(batch)
        calls_made += len(batch)

        # Addresses at the same depth are independent lookups, so they are
        # fetched together rather than one after another. Sequentially this was
        # the whole cost of a trace: an Ethereum address takes ~2.6s (three
        # Etherscan calls - native, ERC-20, internal), so forty of them ran to
        # well over a minute and the dashboard gave up waiting. The work is
        # entirely network-bound, which is what makes threads the right tool.
        skipped = 0
        for address, txs in _fetch_frontier(connector, batch, max_breadth, depth, deadline):
            if txs is _SKIPPED:
                # Not started before the time budget ran out. Budget, not a
                # failure: the provider was never asked.
                skipped += 1
                budget_exhausted = True
                continue
            if txs is None:
                lookup_failures += 1
                continue
            for tx in txs:
                collected.setdefault(f"{tx.chain}:{tx.txid}", tx)
                # Compare in normalised space. Connectors return addresses in
                # their native form (EIP-55 checksummed for ETH), while the
                # frontier holds normalised ones - comparing the two directly
                # silently never matches on ETH.
                inputs_norm = {normalize_address(chain, a) for a in tx.input_addresses}
                if address in inputs_norm:
                    for out in tx.output_addresses:
                        out_norm = normalize_address(chain, out)
                        if out_norm not in seen:
                            next_frontier.append(out_norm)

        # upstream_calls reports lookups actually made.
        calls_made -= skipped

        if next_frontier:
            hops = depth + 1

        # Cap each level as well as the total. Without this a single wide hop
        # could consume the whole budget at depth 1 and report a one-hop trace,
        # which is the least useful shape a forensic answer can take - the
        # money is followed further by going deeper, not by enumerating every
        # sibling of the first hop.
        level_cap = max_breadth * 2
        if len(next_frontier) > level_cap:
            logger.info(
                "trace of %s: depth %d frontier %d addresses, capped to %d",
                root_address,
                depth + 1,
                len(next_frontier),
                level_cap,
            )
            next_frontier = next_frontier[:level_cap]
            frontier_truncated = True

        frontier = next_frontier

    return {
        "transactions": list(collected.values()),
        "addresses_touched": len(seen),
        "hops_discovered": hops,
        # Surfaced, never silent. A trace that stopped early is a materially
        # different finding from one that ran to completion and found nothing,
        # and an investigator has to be able to tell the two apart.
        "upstream_calls": calls_made,
        "budget_exhausted": budget_exhausted,
        "frontier_truncated": frontier_truncated,
        # An address the indexer would not answer for is a hole in the trace,
        # exactly like one the budget skipped. Before this was counted, a
        # throttled Ethereum trace lost most of its addresses and still
        # reported itself complete.
        "lookup_failures": lookup_failures,
        # Not a gap: reaching an exchange is where a trail is supposed to end.
        # Reported so the count is visible, and deliberately not part of
        # `complete`.
        "stopped_at_services": stopped_at_services,
        "complete": not (budget_exhausted or frontier_truncated or lookup_failures),
    }


def _coverage_note(expansion: dict) -> str | None:
    """Say, in plain words, why a trace is not exhaustive - or return None."""
    if expansion["complete"]:
        return None
    reasons = []
    if expansion["budget_exhausted"] or expansion["frontier_truncated"]:
        reasons.append("stopped early to bound upstream API usage")
    if expansion.get("lookup_failures"):
        n = expansion["lookup_failures"]
        reasons.append(
            f"the blockchain data provider did not answer for {n} "
            f"address{'es' if n != 1 else ''}"
        )
    return (
        "Trace is not exhaustive: " + "; ".join(reasons) + ". Findings are valid, "
        "but absence of a result is not evidence of absence."
    )


def detect_mixer_interaction(chain: str, addresses: set[str]) -> bool:
    """True when the traced subgraph touches an address tagged as a mixer.

    Flagged as a risk signal only. Nothing here attempts to de-mix.
    """
    if not addresses:
        return False
    query = """
    MATCH (a:Address)
    WHERE a.chain = $chain AND a.address_norm IN $addresses
      AND (a.is_mixer = true OR EXISTS {
            MATCH (a)-[:TAGGED_AS]->(e:Entity) WHERE e.entity_type = 'mixer'
          })
    RETURN count(a) AS hits
    """
    from app.db.neo4j import get_driver

    with get_driver().session() as session:
        record = session.run(query, chain=chain, addresses=sorted(addresses)).single()
    return bool(record and record["hits"] > 0)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def intake_wallet(
    db: Session,
    address: str,
    victim_ref: str | None = None,
    amount_inr=None,
    narrative: str | None = None,
    ncrp_ref: str | None = None,
    source: str = "manual",
    trace_depth: int | None = None,
    run_clustering: bool = True,
) -> dict:
    """Full intake for one reported wallet. Returns a summary dict."""
    info = detect(address)
    if not info.valid:
        raise IntakeError(info.reason or "invalid address")

    depth = trace_depth or settings.trace_max_depth
    wallet, _created = resolve_or_create_wallet(db, info)

    prior_cases = find_prior_cases(db, wallet)
    duplicate = {
        "is_duplicate": bool(prior_cases),
        "prior_case_count": len(prior_cases),
        "prior_case_numbers": [c.case_number for c in prior_cases],
        "note": (
            f"This address was already reported in {len(prior_cases)} earlier case(s). "
            "Repeat reports of one address across victims are themselves a fraud signal."
            if prior_cases
            else None
        ),
    }

    case = Case(
        case_number=next_case_number(db),
        ncrp_ref=ncrp_ref,
        source=source,
        status="tracing",
        victim_ref=victim_ref,
        amount_inr=amount_inr,
        narrative=narrative,
    )
    db.add(case)
    db.flush()
    db.add(CaseWallet(case_id=case.id, wallet_id=wallet.id, role="reported_suspect"))
    wallet.report_count = len(prior_cases) + 1

    chain = info.chain
    connector = get_connector(chain)
    trace = TraceRun(
        case_id=case.id,
        root_wallet_id=wallet.id,
        max_depth=depth,
        status="running",
        data_source=connector.source_name,
    )
    db.add(trace)
    db.flush()

    try:
        expansion = expand_money_flow(
            connector, info.chain, info.address_norm, depth, settings.trace_max_breadth
        )
        # Score before writing, while full input/output lists are still in
        # hand. The graph keeps only counts and a fee, so scoring later would
        # mean re-fetching from the indexer and spending an API call to
        # re-derive something already known. Returns {} for chains with no
        # trained model, which leaves the property null rather than zero.
        scores = illicit_model.score_transactions(expansion["transactions"])
        write_stats = graph_writer.write_transactions(
            expansion["transactions"],
            data_source=connector.source_name,
            scores=scores,
            model_version=illicit_model.model_info()["version"],
        )
        graph_writer.link_case_to_address(
            case_id=str(case.id),
            case_number=case.case_number,
            reported_at=case.reported_at.isoformat()
            if case.reported_at
            else datetime.now(UTC).isoformat(),
            chain=info.chain,
            address_norm=info.address_norm,
        )

        touched = {info.address_norm}
        for tx in expansion["transactions"]:
            touched.update(normalize_address(chain, a) for a in tx.input_addresses)
            touched.update(normalize_address(chain, a) for a in tx.output_addresses)

        trace.mixer_interaction = detect_mixer_interaction(info.chain, touched)
        trace.hops_discovered = expansion["hops_discovered"]
        trace.addresses_touched = expansion["addresses_touched"]
        # "complete" here means the run finished without error, which is what
        # the trace_status_t enum models. Whether it explored the whole graph
        # is a separate axis, carried in the response as `coverage` - a trace
        # cut short by the call budget must not be presented as exhaustive.
        trace.status = "complete"
        trace.finished_at = datetime.now(UTC)
        if not expansion["complete"]:
            logger.warning(
                "trace of %s was truncated: %d upstream calls, budget_exhausted=%s, "
                "frontier_truncated=%s, lookup_failures=%d",
                info.address_norm,
                expansion["upstream_calls"],
                expansion["budget_exhausted"],
                expansion["frontier_truncated"],
                expansion["lookup_failures"],
            )

        if run_clustering and expansion["transactions"]:
            clustering.run_clustering(info.chain)

    except Exception as exc:  # keep the case; record the failure honestly
        logger.exception("trace failed for %s", info.address_norm)
        trace.status = "failed"
        trace.error = str(exc)[:500]
        trace.finished_at = datetime.now(UTC)
        write_stats = {"transactions": 0}
    finally:
        connector.close()

    wallet.last_traced_at = datetime.now(UTC)
    case.status = "analysed" if trace.status == "complete" else "open"
    db.commit()

    cluster = clustering.get_cluster_for_address(info.chain, info.address_norm) or {}

    return {
        "case_id": case.id,
        "case_number": case.case_number,
        "wallet_id": wallet.id,
        "address": wallet.address,
        "chain": wallet.chain,
        "address_kind": wallet.address_kind,
        "reported_at": case.reported_at,
        "duplicate": duplicate,
        "trace": {
            "trace_run_id": trace.id,
            "status": trace.status,
            "data_source": trace.data_source,
            "max_depth": trace.max_depth,
            "hops_discovered": trace.hops_discovered,
            "addresses_touched": trace.addresses_touched,
            "transactions_ingested": write_stats.get("transactions", 0),
            "mixer_interaction": trace.mixer_interaction,
            "upstream_calls": expansion["upstream_calls"],
            "complete": expansion["complete"],
            "budget_exhausted": expansion["budget_exhausted"],
            "frontier_truncated": expansion["frontier_truncated"],
            "lookup_failures": expansion["lookup_failures"],
            "stopped_at_services": expansion["stopped_at_services"],
            "coverage_note": _coverage_note(expansion),
        },
        "cluster": {
            "cluster_key": cluster.get("cluster_key"),
            "heuristic": cluster.get("heuristic"),
            "size": cluster.get("size", 0),
            "members": cluster.get("members", []),
        },
        "warnings": info.warnings,
        "data_provenance": trace.data_source,
    }
