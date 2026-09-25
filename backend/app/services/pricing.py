"""Fiat valuation for on-chain amounts.

Why this exists: the graph stores native units. 0.4 BTC and 14.8 USDT are not
comparable on any absolute scale, so ranking exposures by volume could only ever
be done *relative to the other candidates in the same analysis*. That works for
ordering within one investigation but means a score of 0.68 in one case says
nothing about a 0.68 in another - and an investigator cannot be told "this
exposure is large" without a unit.

Converting to INR fixes that, and makes the number the one that actually matters
in an Indian cybercrime case file.

Three honesty constraints, because a price is an estimate and this is evidence:

  1. **Every quote records its source and as-of time.** A valuation is only
     meaningful alongside when it was taken.
  2. **Historical price at transfer time is preferred** over today's price. The
     money's worth when it moved is the forensically relevant figure; using
     today's price would revalue a two-year-old transfer at current rates.
  3. **`is_estimate` is always set.** No quote from this module is presented as
     an exact figure, and a missing price degrades to `None` rather than a
     silent zero - scoring then falls back to relative normalisation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings
from app.db import redis_client

logger = logging.getLogger(__name__)
settings = get_settings()

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)

#: WazirX, an Indian exchange, is the first price source. It quotes these
#: assets directly in rupees - the unit the case file uses - so there is no
#: USD/INR conversion step to add its own error, and its public API needs no
#: key. CoinGecko remains the fallback for anything WazirX cannot price.
WAZIRX_BASE = "https://api.wazirx.com/sapi/v1"
WAZIRX_SYMBOLS = {
    "bitcoin": "btcinr",
    "ethereum": "ethinr",
    "tron": "trxinr",
    "tether": "usdtinr",
    "usd-coin": "usdcinr",
}

#: Cache prices for a day. Historical prices never change; current prices move,
#: but not enough to alter a ranking within a day.
CACHE_TTL_SECONDS = 86400
_CACHE_PREFIX = "sih183:price:"

#: Native currency per chain -> CoinGecko id.
NATIVE_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "TRON": "tron"}

#: Token contracts we can value directly. Keyed by the Asset.key format so a
#: lookup is a dict hit, not string parsing.
TOKEN_IDS = {
    "TRON:TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": "tether",       # USDT-TRC20
    "ETH:0xdac17f958d2ee523a2206206994597c13d831ec7": "tether",  # USDT-ERC20
    "ETH:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "usd-coin",  # USDC
}

#: Deterministic prices for DEMO_MODE, so the demo values exposures without a
#: network call and without pretending to be live. Chosen as plausible mid-2026
#: figures; the `source` on every quote says `synthetic`, and the UI repeats it.
SYNTHETIC_INR = {
    "bitcoin": 5_500_000.0,
    "ethereum": 250_000.0,
    "tron": 22.0,
    "tether": 88.0,
    "usd-coin": 88.0,
}


@dataclass(frozen=True)
class PriceQuote:
    """One valuation, with the provenance that makes it usable as evidence."""

    asset_key: str
    inr: float
    source: str
    as_of: str
    is_estimate: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _coin_id(asset_key: str, chain: str) -> str | None:
    """Resolve an asset to a price-feed identifier."""
    if asset_key in TOKEN_IDS:
        return TOKEN_IDS[asset_key]
    if asset_key.endswith(":native"):
        return NATIVE_IDS.get(chain)
    return None


def _cache_get(key: str) -> PriceQuote | None:
    try:
        raw = redis_client.get_client().get(_CACHE_PREFIX + key)
    except Exception:  # noqa: BLE001 - a cache miss must never break valuation
        return None
    if not raw:
        return None
    try:
        return PriceQuote(**json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def _cache_put(key: str, quote: PriceQuote) -> None:
    try:
        redis_client.get_client().setex(
            _CACHE_PREFIX + key, CACHE_TTL_SECONDS, json.dumps(quote.as_dict())
        )
    except Exception:  # noqa: BLE001
        logger.debug("price cache write failed; continuing without cache")


def _wazirx_pacer():
    # WazirX answers a second request inside about a second with "Too many api
    # request". One shared pacer spaces every WazirX call in this process.
    from app.services.connectors.live import _Pacer

    global _WAZIRX_PACER
    if _WAZIRX_PACER is None:
        _WAZIRX_PACER = _Pacer(rate=0.8)
    return _WAZIRX_PACER


_WAZIRX_PACER = None

#: Every WazirX market's last price, from one call, reused briefly. Asking per
#: symbol spent one rate-limited request per coin and still drew 429s; the
#: all-markets endpoint answers every spot quote an analysis needs at once.
_WAZIRX_TICKERS: dict = {"at": 0.0, "prices": {}}
WAZIRX_TICKER_TTL_SECONDS = 60


def _wazirx_spot(client: httpx.Client, symbol: str) -> float | None:
    import threading
    import time as _time

    global _WAZIRX_LOCK
    if _WAZIRX_LOCK is None:
        _WAZIRX_LOCK = threading.Lock()
    with _WAZIRX_LOCK:
        if _time.monotonic() - _WAZIRX_TICKERS["at"] > WAZIRX_TICKER_TTL_SECONDS:
            _wazirx_pacer().wait()
            resp = client.get(f"{WAZIRX_BASE}/tickers/24hr")
            resp.raise_for_status()
            _WAZIRX_TICKERS["prices"] = {
                t["symbol"]: t.get("lastPrice")
                for t in resp.json()
                if isinstance(t, dict) and t.get("symbol")
            }
            _WAZIRX_TICKERS["at"] = _time.monotonic()
        last = _WAZIRX_TICKERS["prices"].get(symbol)
    return float(last) if last else None


_WAZIRX_LOCK = None


def _fetch_wazirx(coin_id: str, at: datetime | None) -> tuple[float, str, str] | None:
    """Return (inr, source, as_of) from WazirX's public INR markets, or None.

    Recent or unspecified times use the market's last trade price. Older
    transfers use the daily closing price for the day the money moved, from
    WazirX's own daily candles - the same "price on the day" rule as the
    CoinGecko path, but already in rupees.
    """
    symbol = WAZIRX_SYMBOLS.get(coin_id)
    if symbol is None:
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            if at is not None and (datetime.now(UTC) - at) > timedelta(days=1):
                _wazirx_pacer().wait()
                day_start = int(datetime(at.year, at.month, at.day, tzinfo=UTC).timestamp())
                resp = client.get(
                    f"{WAZIRX_BASE}/klines",
                    params={"symbol": symbol, "interval": "1d", "startTime": day_start, "limit": 1},
                )
                resp.raise_for_status()
                candles = resp.json()
                # [open_time_seconds, open, high, low, close, volume]
                if isinstance(candles, list) and candles and len(candles[0]) >= 5:
                    opened = int(candles[0][0])
                    # Only accept the candle for the day asked about; WazirX
                    # returns the next available day when a market was quiet.
                    if abs(opened - day_start) < 86400:
                        return float(candles[0][4]), "wazirx_daily_close", at.date().isoformat()
                return None

            last = _wazirx_spot(client, symbol)
            if last:
                return last, "wazirx_spot", datetime.now(UTC).isoformat()
    except httpx.HTTPError as exc:
        logger.warning("WazirX price lookup failed for %s: %s", coin_id, exc)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        logger.warning("unexpected WazirX payload for %s: %s", coin_id, exc)
    return None


def _fetch_live(coin_id: str, at: datetime | None) -> tuple[float, str, str] | None:
    """Return (inr, source, as_of) from WazirX, falling back to CoinGecko."""
    quoted = _fetch_wazirx(coin_id, at)
    if quoted is not None:
        return quoted
    return _fetch_coingecko(coin_id, at)


def _fetch_coingecko(coin_id: str, at: datetime | None) -> tuple[float, str, str] | None:
    """Return (inr, source, as_of) from CoinGecko, or None."""
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            if at is not None and (datetime.now(UTC) - at) > timedelta(days=1):
                # Historical: the price on the day the money actually moved.
                resp = client.get(
                    f"{COINGECKO_BASE}/coins/{coin_id}/history",
                    params={"date": at.strftime("%d-%m-%Y"), "localization": "false"},
                )
                resp.raise_for_status()
                data = resp.json()
                inr = (
                    data.get("market_data", {})
                    .get("current_price", {})
                    .get("inr")
                )
                if inr:
                    return float(inr), "coingecko_historical", at.date().isoformat()
                return None

            resp = client.get(
                f"{COINGECKO_BASE}/simple/price",
                params={"ids": coin_id, "vs_currencies": "inr"},
            )
            resp.raise_for_status()
            inr = resp.json().get(coin_id, {}).get("inr")
            if inr:
                return float(inr), "coingecko_spot", datetime.now(UTC).isoformat()
    except httpx.HTTPError as exc:
        logger.warning("price lookup failed for %s: %s", coin_id, exc)
    except (ValueError, KeyError) as exc:
        logger.warning("unexpected price payload for %s: %s", coin_id, exc)
    return None


def get_price(asset_key: str, chain: str, at: datetime | None = None) -> PriceQuote | None:
    """INR value of one unit of `asset_key`, at `at` if a historical price exists.

    Returns None when the asset cannot be valued - callers must handle that
    rather than treating a missing price as zero, which would silently rank a
    valuable exposure as worthless.
    """
    coin_id = _coin_id(asset_key, chain)
    if coin_id is None:
        logger.debug("no price feed mapping for %s", asset_key)
        return None

    day = at.date().isoformat() if at else "spot"
    cache_key = f"{coin_id}:{day}"

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if settings.demo_mode:
        inr = SYNTHETIC_INR.get(coin_id)
        if inr is None:
            return None
        quote = PriceQuote(
            asset_key=asset_key,
            inr=inr,
            source="synthetic",
            as_of=day,
            is_estimate=True,
        )
    else:
        fetched = _fetch_live(coin_id, at)
        if fetched is None:
            return None
        inr, source, as_of = fetched
        quote = PriceQuote(
            asset_key=asset_key, inr=inr, source=source, as_of=as_of, is_estimate=True
        )

    _cache_put(cache_key, quote)
    return quote


def to_inr(
    amount: float, asset_key: str, chain: str, at: datetime | None = None
) -> tuple[float | None, PriceQuote | None]:
    """Convert an on-chain amount to INR. Returns (value, quote used)."""
    quote = get_price(asset_key, chain, at)
    if quote is None:
        return None, None
    return amount * quote.inr, quote
