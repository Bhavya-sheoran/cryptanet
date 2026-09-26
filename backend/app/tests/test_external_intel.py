"""Arkham, Chainabuse, WazirX and Binance Proof of Reserves.

No network: every provider response is scripted. What these pin is the part
that matters for evidence - that each source is parsed correctly, that a
missing key or a failed call is reported as "not checked" rather than as a
negative finding, and that the sources are consulted in the right order.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services import attribution, external_intel, pricing, reports


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    store = {}
    monkeypatch.setattr(external_intel, "_cache_get", lambda k: store.get(k))
    monkeypatch.setattr(external_intel, "_cache_put", lambda k, v, ttl: store.__setitem__(k, v))
    # The Chainabuse monthly quota counts in Redis, which is shared with the
    # running deployment. Left alone, a stack that has done real lookups this
    # month makes every test here return `budget_exhausted` and fail for a
    # reason that has nothing to do with what it is testing. The gate itself is
    # covered by test_a_spent_monthly_budget_is_not_reported_as_no_reports.
    monkeypatch.setattr(external_intel, "_spend_monthly_budget", lambda: True)
    return store


def settings_with(monkeypatch, **values):
    s = external_intel.get_settings()
    for k, v in values.items():
        monkeypatch.setattr(s, k, v)
    return s


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)


# ---------------------------------------------------------------------------
# Arkham
# ---------------------------------------------------------------------------
ARKHAM_ALL = {
    "bitcoin": {"address": "x", "chain": "bitcoin"},
    "ethereum": {
        "address": "0xabc",
        "chain": "ethereum",
        "arkhamEntity": {"id": "kraken", "name": "Kraken", "type": "cex"},
        "arkhamLabel": {"name": "Kraken Hot Wallet 7"},
    },
}


def test_arkham_entity_is_read_from_the_chain_asked_about():
    found = external_intel.parse_arkham(ARKHAM_ALL, "ETH")
    assert found["entity_name"] == "Kraken"
    assert found["entity_type"] == "exchange"
    assert found["label"] == "Kraken Hot Wallet 7"


def test_a_decentralised_exchange_is_not_labelled_an_exchange():
    # "exchange" tells an officer a legal request can identify the customer.
    # A DEX cannot answer one, so it keeps its name but not that type.
    payload = {"ethereum": {"arkhamEntity": {"name": "Uniswap", "type": "dex"}}}
    assert external_intel.parse_arkham(payload, "ETH")["entity_type"] == "unknown"


def test_a_label_without_an_entity_is_not_an_attribution():
    payload = {"ethereum": {"arkhamLabel": {"name": "Some deposit"}}}
    assert external_intel.parse_arkham(payload, "ETH") is None


def test_arkham_is_not_called_without_a_key(monkeypatch):
    settings_with(monkeypatch, arkham_api_key="")
    monkeypatch.setattr(external_intel.httpx, "get",
                        lambda *a, **k: pytest.fail("called Arkham with no key"))
    assert external_intel.arkham_lookup("ETH", "0xabc") is None


def test_an_arkham_hit_is_stored_as_a_tag_and_a_miss_is_remembered(monkeypatch, no_cache):
    settings_with(monkeypatch, arkham_api_key="k")
    stored, calls = [], []
    monkeypatch.setattr(external_intel, "persist_tag", lambda c, a, f: stored.append((c, a, f)))

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        assert headers == {"API-Key": "k"}
        return FakeResponse(ARKHAM_ALL if "0xabc" in url else {"ethereum": {}})

    monkeypatch.setattr(external_intel.httpx, "get", fake_get)

    assert external_intel.arkham_lookup("ETH", "0xabc")["entity_name"] == "Kraken"
    assert stored and stored[0][:2] == ("ETH", "0xabc")

    assert external_intel.arkham_lookup("ETH", "0xmiss") is None
    assert external_intel.arkham_lookup("ETH", "0xmiss") is None
    assert sum("0xmiss" in u for u in calls) == 1, "a miss was asked twice"


def test_a_failed_arkham_call_is_not_remembered_as_a_miss(monkeypatch, no_cache):
    import httpx

    settings_with(monkeypatch, arkham_api_key="k")

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(external_intel.httpx, "get", boom)
    assert external_intel.arkham_lookup("ETH", "0xabc") is None
    assert no_cache == {}, "an outage was cached as 'not identified'"


# ---------------------------------------------------------------------------
# Attribution order: curated tag -> Arkham -> behaviour
# ---------------------------------------------------------------------------
def test_curated_tags_win_over_arkham(monkeypatch):
    monkeypatch.setattr(attribution, "lookup_tags", lambda c, a: [{
        "entity_name": "Binance", "entity_type": "exchange", "source": "binance_por",
        "confidence": 1.0, "matched_address": a, "cluster_key": None, "cluster_size": 1}])
    monkeypatch.setattr(external_intel, "arkham_lookup",
                        lambda c, a: pytest.fail("asked Arkham about a tagged wallet"))
    assert attribution.attribute("BTC", "addr").method == "tagged_db"


def test_arkham_names_what_the_tag_database_does_not_know(monkeypatch):
    monkeypatch.setattr(attribution, "lookup_tags", lambda c, a: [])
    monkeypatch.setattr(external_intel, "arkham_lookup", lambda c, a: {
        "entity_name": "Kraken", "entity_type": "exchange", "label": "Kraken"})
    result = attribution.attribute("ETH", "0xabc")
    assert (result.method, result.entity_name, result.source) == ("arkham", "Kraken", "arkham")


def test_without_arkham_the_behavioural_tier_still_runs(monkeypatch):
    monkeypatch.setattr(attribution, "lookup_tags", lambda c, a: [])
    monkeypatch.setattr(external_intel, "arkham_lookup", lambda c, a: None)
    monkeypatch.setattr(attribution, "behavioural_features", lambda c, a: {})
    monkeypatch.setattr(attribution, "classify_category", lambda f: ("exchange", 0.4, "busy"))
    assert attribution.attribute("ETH", "0xabc").method == "classifier"


# ---------------------------------------------------------------------------
# Chainabuse
# ---------------------------------------------------------------------------
def test_no_key_means_not_checked_never_zero_reports(monkeypatch):
    settings_with(monkeypatch, chainabuse_api_key="")
    result = external_intel.chainabuse_reports("TRON", "Tabc")
    assert result["status"] == "not_configured"
    assert "report_count" not in result


def test_reports_are_summarised_with_categories_and_verification(monkeypatch):
    settings_with(monkeypatch, chainabuse_api_key="k")
    payload = {"count": 3, "reports": [
        {"id": "1", "scamCategory": "PIG_BUTCHERING", "checked": True, "trusted": True,
         "createdAt": "2026-08-01T00:00:00Z"},
        {"id": "2", "scamCategory": "PIG_BUTCHERING", "createdAt": "2026-09-01T00:00:00Z"},
        {"id": "3", "scamCategory": "PHISHING"},
    ]}
    seen = {}

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        seen.update(url=url, params=params, auth=auth)
        return FakeResponse(payload)

    monkeypatch.setattr(external_intel.httpx, "get", fake_get)
    result = external_intel.chainabuse_reports("TRON", "Tabc")

    assert seen["auth"] == ("k", "k") and seen["params"]["address"] == "Tabc"
    assert result["status"] == "checked" and result["report_count"] == 3
    assert result["verified_reports"] == 1
    assert list(result["categories"]) == ["PIG_BUTCHERING", "PHISHING"]
    assert result["latest_report_at"] == "2026-09-01T00:00:00Z"


def test_a_failed_lookup_is_unavailable_not_clean(monkeypatch):
    import httpx

    settings_with(monkeypatch, chainabuse_api_key="k")

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(external_intel.httpx, "get", boom)
    assert external_intel.chainabuse_reports("ETH", "0xabc")["status"] == "unavailable"


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (None, "not checked - no Chainabuse API key configured"),
        ({"status": "unavailable"}, "not checked - Chainabuse did not respond"),
        ({"status": "checked", "report_count": 0},
         "checked - no reports filed against this wallet"),
        ({"status": "checked", "report_count": 2, "verified_reports": 1,
          "categories": {"PHISHING": 2}},
         "2 report(s) filed against this wallet, 1 verified by Chainabuse - PHISHING (2)"),
    ],
)
def test_case_file_line_never_turns_not_checked_into_zero(summary, expected):
    assert reports._scam_report_line(summary) == expected


class NoPacer:
    def wait(self):
        pass


# ---------------------------------------------------------------------------
# WazirX pricing
# ---------------------------------------------------------------------------
def test_wazirx_is_asked_before_coingecko(monkeypatch):
    monkeypatch.setattr(pricing, "_fetch_wazirx", lambda c, at: (99.27, "wazirx_spot", "now"))
    monkeypatch.setattr(pricing, "_fetch_coingecko",
                        lambda c, at: pytest.fail("fell back although WazirX answered"))
    assert pricing._fetch_live("tether", None) == (99.27, "wazirx_spot", "now")


def test_coingecko_covers_what_wazirx_cannot_price(monkeypatch):
    monkeypatch.setattr(pricing, "_fetch_wazirx", lambda c, at: None)
    monkeypatch.setattr(pricing, "_fetch_coingecko", lambda c, at: (1.0, "coingecko_spot", "now"))
    assert pricing._fetch_live("tron", None)[1] == "coingecko_spot"


def test_an_old_transfer_uses_the_daily_close_for_that_day(monkeypatch):
    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            assert url.endswith("/klines") and params["symbol"] == "trxinr"
            return FakeResponse([[params["startTime"], 33.1, 33.4, 32.7, 32.78, 15574.6]])

    monkeypatch.setattr(pricing.httpx, "Client", Client)
    monkeypatch.setattr(pricing, "_wazirx_pacer", NoPacer)
    at = datetime.now(UTC) - timedelta(days=40)
    inr, source, as_of = pricing._fetch_wazirx("tron", at)
    assert (inr, source, as_of) == (32.78, "wazirx_daily_close", at.date().isoformat())


def test_a_candle_for_a_different_day_is_not_used(monkeypatch):
    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            # A quiet market: WazirX hands back the NEXT day that traded.
            return FakeResponse([[params["startTime"] + 5 * 86400, 1, 1, 1, 50.0, 1]])

    monkeypatch.setattr(pricing.httpx, "Client", Client)
    monkeypatch.setattr(pricing, "_wazirx_pacer", NoPacer)
    assert pricing._fetch_wazirx("tron", datetime.now(UTC) - timedelta(days=40)) is None


# ---------------------------------------------------------------------------
# Binance Proof of Reserves
# ---------------------------------------------------------------------------
def _por_module():
    path = Path("/app/scripts/sync_binance_por.py")
    if not path.exists():
        path = Path(__file__).resolve().parents[3] / "scripts" / "sync_binance_por.py"
    spec = importlib.util.spec_from_file_location("sync_binance_por", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_por_list_becomes_binance_exchange_tags_on_traceable_chains_only():
    por = _por_module()
    tags = por.to_tags([
        {"network": "BTC", "address": "12VuUfQHTGqWDvBzm8TBad1mZBm4hjGEzN",
         "thirdPartyCustodianName": ""},
        {"network": "ETH", "address": "0xABCDEF", "thirdPartyCustodianName": "Ceffu"},
        {"network": "ETH", "address": "0xabcdef", "thirdPartyCustodianName": "Ceffu"},  # repeat
        {"network": "TRX", "address": "TAzsQ9Gx8eqFNFSKbeXrbi45CuVPHzA8wr",
         "thirdPartyCustodianName": ""},
        {"network": "SOL", "address": "So1ana", "thirdPartyCustodianName": ""},  # no connector
    ])
    by_chain = {t["chain"]: t for t in tags}

    assert sorted(by_chain) == ["BTC", "ETH", "TRON"]
    assert all(t["entity_name"] == "Binance" and t["entity_type"] == "exchange" for t in tags)
    assert all(t["source"] == "binance_por" and t["confidence"] == 1.0 for t in tags)
    assert by_chain["ETH"]["address_norm"] == "0xabcdef"
    assert "custodian Ceffu" in by_chain["ETH"]["label"]


def test_a_spent_monthly_budget_is_not_reported_as_no_reports(monkeypatch):
    """A quota that ran out must read as "not checked", never as "clean".

    Chainabuse's free tier is metered per month, so this path is reached in
    normal operation rather than only under failure. Reporting it as zero
    reports would tell an officer the wallet has no complaints against it on the
    strength of a lookup that never happened.
    """
    settings_with(monkeypatch, chainabuse_api_key="k")
    monkeypatch.setattr(external_intel, "_spend_monthly_budget", lambda: False)

    def must_not_be_called(*_a, **_k):  # pragma: no cover - the assert is the point
        raise AssertionError("no HTTP call may be made once the quota is spent")

    monkeypatch.setattr(external_intel.httpx, "get", must_not_be_called)

    result = external_intel.chainabuse_reports("TRON", "Tabc")
    assert result["status"] == "budget_exhausted"
    assert result.get("report_count") is None
    assert "not checked" in result["note"]
