"""Shared test fixtures.

Clustering is Cypher and GDS, so testing it against a mock would test the mock.
These tests run against the real Neo4j from the compose stack and skip cleanly
when it is not reachable.
"""

from __future__ import annotations

import os

# Captured BEFORE the override below, because it describes the DEPLOYMENT this
# suite has been pointed at rather than the mode the suite itself runs in.
# `pytest_configure` refuses to run against a live one - see the note there.
_DEPLOYMENT_DEMO_MODE = os.environ.get("DEMO_MODE", "true").strip().lower()

# Set before anything imports app.config, whose settings are lru_cached at first
# use. The whole suite shares one TestClient identity, so the per-IP limiter
# would see a few hundred requests from a single "client" in well under a
# minute and start returning 429 to tests that are about correctness, not
# throughput. The limiter itself is covered by test_middleware.py, which builds
# an app with it switched on.
# Assigned, not setdefault: docker-compose sets both of these in the container
# environment, so setdefault silently deferred to compose and the suite ran
# with the limiter ON. That went unnoticed until a burst of intentionally
# failing logins tripped the lockout and took 50 unrelated tests with it. The
# test environment has to win over the deployment environment here.
os.environ["RATE_LIMIT_ENABLED"] = "false"

# The suite must never call a live indexer. Real API calls would make it slow,
# flaky, dependent on somebody's network, and would burn a rate-limited quota
# that belongs to an investigation. The live path is verified deliberately and
# by hand (see scripts/inspect_live_tx.py), never as a side effect of `pytest`.
os.environ["DEMO_MODE"] = "true"

# Most API tests sign in through the demo accounts, so the suite runs with that
# path enabled. test_demo_auth.py asserts the gate itself, with the flag off.
os.environ["ALLOW_DEMO_AUTH"] = "true"

from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from app.services.connectors.base import Asset, ChainTransaction, TxIO  # noqa: E402

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)

#: Escape hatch, for deliberately running the suite against a live-configured
#: deployment that you know holds nothing you need.
LIVE_OVERRIDE_ENV = "ALLOW_TESTS_ON_LIVE_DEPLOYMENT"


def pytest_configure(config):
    """Refuse to run against a deployment configured for real chain data.

    The suite is destructive by design: the graph fixture calls
    `clear_test_data()`, which deletes every transaction, cluster, case and
    untagged address. Neo4j Community has a single database, so there is no
    separation between "the test graph" and "the graph holding real traced
    evidence" - running pytest against a live deployment destroys open
    investigations.

    It is not only the graph. The suite forces ALLOW_DEMO_AUTH on and seeds the
    demo accounts, and `seed_demo_users` re-enables ones that were deactivated.
    A run against a live deployment therefore silently restores the
    `supervisor` login - whose password is published in this repository, and
    whose role is the one that approves freezes.

    Both were observed, not theorised: a run against the live stack wiped the
    traced graph and turned both demo accounts back on.
    """
    if _DEPLOYMENT_DEMO_MODE in {"false", "0", "no"} and not os.environ.get(LIVE_OVERRIDE_ENV):
        raise pytest.UsageError(
            "Refusing to run the test suite against a deployment with "
            "DEMO_MODE=false.\n"
            "  The suite deletes traced graph data and re-enables the demo "
            "accounts, including the supervisor login that approves freezes.\n"
            "  Run the tests against a demo/CI stack instead. To override "
            f"anyway, set {LIVE_OVERRIDE_ENV}=1."
        )


@pytest.fixture(scope="session")
def neo4j_available() -> bool:
    from app.db import neo4j as neo4j_db

    try:
        neo4j_db.ping()
        return True
    except Exception:
        return False


@pytest.fixture
def graph(neo4j_available):
    """Empty graph before and after each test that uses it."""
    if not neo4j_available:
        pytest.skip("Neo4j not reachable - start the compose stack to run graph tests")

    from app.services import graph_writer

    # Scoped cleanup: a full wipe would destroy the seeded tagged-address
    # database (OFAC / Etherscan labels / WalletExplorer / TagPacks), which is
    # reference data rather than test data.
    graph_writer.clear_test_data()
    yield graph_writer
    graph_writer.clear_test_data()


def tx(
    txid: str,
    inputs: list[tuple[str, float]],
    outputs: list[tuple[str, float]],
    minutes: int = 0,
    chain: str = "BTC",
    asset: Asset | None = None,
    status: str = "success",
) -> ChainTransaction:
    """Build a ChainTransaction concisely. `minutes` offsets from BASE_TIME."""
    return ChainTransaction(
        chain=chain,
        txid=txid,
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        block_height=800_000 + minutes,
        fee=Decimal("0.0001"),
        asset=asset or Asset.native(chain),
        status=status,
        inputs=[TxIO(address=a, value=Decimal(str(v)), index=i) for i, (a, v) in enumerate(inputs)],
        outputs=[
            TxIO(address=a, value=Decimal(str(v)), index=i) for i, (a, v) in enumerate(outputs)
        ],
    )
