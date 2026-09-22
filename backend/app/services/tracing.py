"""Trace path assembly for the investigator view.

The graph already holds the money flow; this turns it into the ordered hop
sequence and the node/link sets the Sankey visualisation consumes in Phase 3.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.db.neo4j import get_driver
from app.services.connectors.base import SOURCE_SYNTHETIC

logger = logging.getLogger(__name__)

# Shortest path to each reachable address, so the Sankey shows the primary route
# rather than every permutation of a dense subgraph.
_TRACE_PATH = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
CALL (root) {
  MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(dest:Address))
  WHERE dest <> root
  RETURN p, dest
  LIMIT $max_nodes
}
WITH root, p, dest
OPTIONAL MATCH (dest)-[:TAGGED_AS]->(e:Entity)
OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
RETURN dest.address_norm AS address,
       length(p)         AS hop,
       [n IN nodes(p) | n.address_norm] AS path,
       [r IN relationships(p) | {
          txid: r.txid, value: r.value,
          timestamp: toString(r.timestamp), asset: r.asset,
          data_source: coalesce(r.data_source, 'unknown')
       }] AS transfers,
       e.name        AS entity_name,
       e.entity_type AS entity_type,
       cl.cluster_key AS cluster_key
ORDER BY hop, address
"""

#
# `dest` is bound through its tag BEFORE the shortestPath rather than after.
# Two reasons, both learned from real chain data:
#   * Neo4j refuses a shortestPath whose start and end are the same node, and
#     on a live chain a wallet does send back to itself. Binding dest first
#     lets `dest <> root` exclude that case before the search runs; filtering
#     afterwards is too late and the query dies with a DatabaseError.
#   * Tagged addresses are a tiny fraction of the graph, so starting from them
#     is also the more selective plan.
_TERMINALS = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
MATCH (dest:Address)-[:TAGGED_AS]->(e:Entity)
WHERE e.entity_type IN ['exchange', 'sanctioned', 'payment_processor', 'gambling']
  AND dest <> root
MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(dest))
OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
RETURN dest.address_norm AS address, length(p) AS hop,
       e.name AS entity_name, e.entity_type AS entity_type,
       cl.cluster_key AS cluster_key
ORDER BY hop ASC
"""

# Endpoints of the flow: nothing leaves them within the traced subgraph.
#
# Reachable addresses are enumerated from the root and only then filtered down
# to the ones with no onward transfer. The other way round - scanning every
# address on the chain for a dead end - reads the whole graph: on live data
# that is tens of thousands of nodes per chain and the query stops returning
# in reasonable time.
_SINKS = """
MATCH (root:Address {chain: $chain, address_norm: $address_norm})
CALL (root) {
  MATCH p = shortestPath((root)-[:TRANSFERRED*1..%(depth)d]->(dest:Address))
  WHERE dest <> root
  RETURN p, dest
  LIMIT 500
}
WITH root, p, dest
WHERE NOT EXISTS { MATCH (dest)-[:TRANSFERRED]->(:Address) }
OPTIONAL MATCH (dest)-[:MEMBER_OF]->(cl:Cluster)
RETURN dest.address_norm AS address, length(p) AS hop,
       cl.cluster_key AS cluster_key
ORDER BY hop DESC
LIMIT 25
"""


def trace_path(
    chain: str, address_norm: str, depth: int = 6, max_nodes: int | None = None
) -> dict:
    """Ordered hops reachable from `address_norm`, plus Sankey-ready node/link sets.

    `max_nodes` defaults to the configured budget. It caps the diagram, not the
    investigation: tagged destinations come from `terminal_attributions` and
    service exposure from its own query, both of which are selective and
    unaffected by this limit. When the budget bites, `truncated` says so.
    """
    if max_nodes is None:
        max_nodes = get_settings().graph_max_nodes
    query = _TRACE_PATH % {"depth": max(1, min(depth, 8))}
    with get_driver().session() as session:
        rows = [
            dict(r)
            for r in session.run(
                query, chain=chain, address_norm=address_norm, max_nodes=max_nodes
            )
        ]

    nodes: dict[str, dict] = {
        address_norm: {"address": address_norm, "hop": 0, "role": "reported_suspect"}
    }
    links: dict[tuple[str, str, str], dict] = {}

    for row in rows:
        addr = row["address"]
        nodes.setdefault(
            addr,
            {
                "address": addr,
                "hop": row["hop"],
                "role": "terminal" if row["entity_name"] else "intermediate",
                "entity_name": row["entity_name"],
                "entity_type": row["entity_type"],
                "cluster_key": row["cluster_key"],
            },
        )
        path, transfers = row["path"], row["transfers"]
        for i, transfer in enumerate(transfers):
            src, dst = path[i], path[i + 1]
            nodes.setdefault(src, {"address": src, "hop": i, "role": "intermediate"})
            nodes.setdefault(dst, {"address": dst, "hop": i + 1, "role": "intermediate"})
            key = (src, dst, transfer.get("txid") or "")
            if key not in links:
                links[key] = {
                    "source": src,
                    "target": dst,
                    "txid": transfer.get("txid"),
                    "value": transfer.get("value"),
                    "timestamp": transfer.get("timestamp"),
                    "asset": transfer.get("asset"),
                }

    # Provenance of the edges this trace actually crossed, read off the edges
    # rather than inferred from DEMO_MODE. A path that touches even one
    # synthetic edge is not a live-data finding, and the caller has to be able
    # to see that without trusting a global flag.
    edge_sources = sorted(
        {
            transfer.get("data_source") or "unknown"
            for r in rows
            for transfer in (r["transfers"] or [])
        }
    )

    return {
        "root": address_norm,
        "chain": chain,
        "depth": depth,
        "hops": [
            {
                "address": r["address"],
                "hop": r["hop"],
                "entity_name": r["entity_name"],
                "entity_type": r["entity_type"],
                "cluster_key": r["cluster_key"],
            }
            for r in rows
        ],
        "nodes": sorted(nodes.values(), key=lambda n: (n["hop"], n["address"])),
        "links": list(links.values()),
        "node_count": len(nodes),
        "link_count": len(links),
        "truncated": len(rows) >= max_nodes,
        "edge_sources": edge_sources,
        "contains_synthetic": SOURCE_SYNTHETIC in edge_sources,
        "provenance_complete": "unknown" not in edge_sources,
    }


def terminal_attributions(chain: str, address_norm: str, depth: int = 6) -> list[dict]:
    """Tagged services reachable from the reported address, nearest hop first."""
    query = _TERMINALS % {"depth": max(1, min(depth, 8))}
    with get_driver().session() as session:
        rows = [dict(r) for r in session.run(query, chain=chain, address_norm=address_norm)]

    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["address"] in seen:
            continue
        seen.add(row["address"])
        unique.append(row)
    return unique


def sink_addresses(chain: str, address_norm: str, depth: int = 6) -> list[dict]:
    """Where the traced flow stops within the subgraph we hold."""
    query = _SINKS % {"depth": max(1, min(depth, 8))}
    with get_driver().session() as session:
        return [dict(r) for r in session.run(query, chain=chain, address_norm=address_norm)]
