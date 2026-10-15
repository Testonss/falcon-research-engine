"""Provider-driven asset discovery. No hardcoded universe.

Binance: public exchangeInfo catalog (spot, TRADING, allowlisted quotes)
plus the all-symbols 24h ticker snapshot (one HTTP call) for live
quote/change/volume across the whole catalog.

Solana: per-mint resolution through the existing documented backends;
the tracked universe is the user-configured mint set (CLI seeds, UI
additions, persisted watchlist) — never a fabricated global list.

All fetchers take an injectable http_get(url, timeout) -> (status, body)
so tests run offline with fixtures. Never raises on malformed payloads
where a (results, stats) pair can honestly report the damage; raises
IOError only when the transport itself fails after bounded retries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from falcon_terminal.registry import (
    BINANCE_QUOTES,
    CanonicalAsset,
    binance_id,
)
from falcon_terminal.timeframes import TIMEFRAMES
from market_data.providers.binance import (
    _default_http_get as _binance_http_get,
)
from market_data.providers.binance import (
    parse_klines,
    parse_ticker_24hr,
    to_falcon_symbol,
)
from market_data.providers.solana import rank_pools

logger = logging.getLogger(__name__)

HttpGet = Callable[[str, float], Tuple[int, str]]

BINANCE_REST = "https://api.binance.com"
DEXSCREENER = "https://api.dexscreener.com"

MAX_POOLS_PER_MINT = 8
MAX_TRACKED_SOLANA = 25

# Terminal timeframe -> native provider interval (Binance is case-sensitive:
# daily is "1d", never "1D"). Display labels stay canonical in TIMEFRAMES.
NATIVE_INTERVAL = {"1D": "1d"}


def to_native_interval(timeframe: str) -> str:
    """Map a terminal timeframe to the provider's native interval string."""
    from falcon_terminal.timeframes import TIMEFRAMES
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return NATIVE_INTERVAL.get(timeframe, timeframe)


def parse_exchange_info(payload: Any) -> Tuple[List[CanonicalAsset], Dict[str, int]]:
    """exchangeInfo -> spot CanonicalAssets. Returns (assets, stats).

    Keeps only status==TRADING spot symbols with allowlisted quotes.
    stats: {symbols, kept, skipped}.
    """
    stats = {"symbols": 0, "kept": 0, "skipped": 0}
    assets: List[CanonicalAsset] = []
    try:
        symbols = payload.get("symbols", [])
    except AttributeError:
        return [], stats
    if not isinstance(symbols, list):
        return [], stats
    for entry in symbols:
        stats["symbols"] += 1
        try:
            if not isinstance(entry, dict):
                raise ValueError("not a dict")
            if entry.get("status") != "TRADING":
                raise ValueError("not trading")
            if not entry.get("isSpotTradingAllowed", False):
                raise ValueError("not spot")
            raw = str(entry.get("symbol", ""))
            base = str(entry.get("baseAsset", ""))
            quote = str(entry.get("quoteAsset", ""))
            if quote not in BINANCE_QUOTES or not base:
                raise ValueError("quote not tracked")
            display = to_falcon_symbol(raw)  # may raise -> skip
            assets.append(CanonicalAsset(
                id=binance_id(raw), provider="binance", venue="spot",
                chain="", symbol=display, name="", mint=None, quote=quote,
                kind="asset", parent_id=None, supported=TIMEFRAMES))
            stats["kept"] += 1
        except (ValueError, TypeError, AttributeError):
            stats["skipped"] += 1
    return assets, stats


def parse_all_tickers(payload: Any) -> Tuple[Dict[str, dict], Dict[str, int]]:
    """All-symbols 24h array -> {BINANCESYM: {price, change, quote_volume, ts}}.

    stats: {rows, kept, skipped}. Reuses the validated single-ticker parser.
    """
    stats = {"rows": 0, "kept": 0, "skipped": 0}
    out: Dict[str, dict] = {}
    if not isinstance(payload, list):
        return out, stats
    for row in payload:
        stats["rows"] += 1
        try:
            if not isinstance(row, dict):
                raise ValueError("not a dict")
            t = parse_ticker_24hr(row)
            if t is None:
                raise ValueError("unparseable")
            raw = str(row.get("symbol", ""))
            qv = None
            try:
                qv = float(row.get("quoteVolume", 0.0))
                import math
                if not math.isfinite(qv) or qv < 0:
                    qv = None
            except (TypeError, ValueError):
                qv = None
            out[raw] = {"price": t.price, "change": t.change_pct_24h,
                        "quote_volume": qv, "event_time": t.timestamp}
            stats["kept"] += 1
        except (ValueError, TypeError, AttributeError):
            stats["skipped"] += 1
    return out, stats


async def _fetch_json(http_get: HttpGet, url: str, timeout: float,
                      retries: int = 3, max_bytes: int = 4 * 1024 * 1024) -> Any:
    last_err: Optional[Exception] = None
    delay = 0.5
    for attempt in range(1, max(1, retries) + 1):
        try:
            status, body = await asyncio.to_thread(http_get, url, timeout)
            if status != 200:
                raise IOError(f"HTTP {status}")
            if len(body) > max_bytes:
                raise IOError(f"response too large (>{max_bytes // 1024 // 1024}MB)")
            return json.loads(body)
        except (IOError, ValueError, OSError) as e:
            last_err = e
            logger.warning("discovery GET attempt %d failed: %s", attempt, e)
            if attempt < retries:
                await asyncio.sleep(min(delay, 8.0))
                delay *= 2
    raise IOError(f"discovery GET failed after {retries}: {last_err}")


async def fetch_spot_catalog(http_get: Optional[HttpGet] = None,
                             timeout: float = 10.0) -> Tuple[List[CanonicalAsset], Dict[str, int]]:
    """Fetch + parse Binance spot catalog (public, no key, bounded).

    exchangeInfo covers every Binance market (~18MB / ~3700 symbols, weight
    20); the cap fits the real payload and it is fetched rarely (hourly).
    Only a few hundred spot instruments survive filtering.
    """
    payload = await _fetch_json(http_get or _binance_http_get,
                                BINANCE_REST + "/api/v3/exchangeInfo", timeout,
                                max_bytes=24 * 1024 * 1024)
    return parse_exchange_info(payload)


async def fetch_all_tickers(http_get: Optional[HttpGet] = None,
                            timeout: float = 10.0) -> Tuple[Dict[str, dict], Dict[str, int]]:
    """Fetch + parse the all-symbols 24h snapshot (public, no key, bounded)."""
    payload = await _fetch_json(
        http_get or _binance_http_get,
        BINANCE_REST + "/api/v3/ticker/24hr", timeout)
    return parse_all_tickers(payload)


async def fetch_klines(symbol: str, interval: str, limit: int = 200,
                       http_get: Optional[HttpGet] = None,
                       timeout: float = 10.0) -> Tuple[List, int]:
    """Fetch + validate klines for any supported interval (public, bounded).

    Returns (candles, rejected). The last bar may still be forming; callers
    must check close_time vs now (see mark_forming).
    """
    from market_data.providers.binance import to_binance_symbol
    from falcon_terminal.timeframes import TIMEFRAMES
    if interval not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {interval!r}")
    bsym = to_binance_symbol(symbol)
    limit = max(2, min(int(limit), 1000))
    qs = urllib.parse.urlencode({"symbol": bsym,
                                 "interval": to_native_interval(interval),
                                 "limit": limit})
    payload = await _fetch_json(http_get or _binance_http_get,
                                BINANCE_REST + "/api/v3/klines?" + qs, timeout)
    try:
        display = to_falcon_symbol(bsym)
    except ValueError:
        display = bsym
    return parse_klines(payload, display, interval)


def mark_forming(candles: List, now_ms: Optional[int] = None) -> Tuple[List, Optional[object]]:
    """Split fetched klines into (closed, forming_or_None).

    A bar whose close_time is missing or in the future is provisional and
    must never be stored as finalized history.
    """
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    closed = []
    forming = None
    for c in candles:
        try:
            ct = c.close_time.timestamp() * 1000 if c.close_time else None
        except Exception:
            ct = None
        if ct is None or ct > now_ms:
            # Provisional: flagged forming, never stored as finalized history.
            # Keep the latest forming bar; earlier ones are superseded.
            c.is_closed = False
            if forming is None or c.timestamp >= forming.timestamp:
                forming = c
        else:
            c.is_closed = True
            closed.append(c)
    closed.sort(key=lambda b: b.timestamp)
    return closed, forming


def discover_pools(mint: str, pairs: List[dict],
                   limit: int = MAX_POOLS_PER_MINT) -> List[CanonicalAsset]:
    """Top-N pools for a mint as pool-level registry entries (deterministic)."""
    from falcon_terminal.registry import solana_pool_id
    out: List[CanonicalAsset] = []
    for p in rank_pools(pairs or [], mint, limit=limit):
        try:
            dex = str(p.get("dexId", "") or "unknown")
            pool = str(p.get("pairAddress", ""))
            if not pool:
                continue
            base = p.get("baseToken", {}) or {}
            quote = p.get("quoteToken", {}) or {}
            if str(base.get("address", "")) == mint:
                sym = str(base.get("symbol", "") or mint[:8])
                q = str(quote.get("symbol", "") or "")
                name = str(base.get("name", "") or "")
            else:
                sym = str(quote.get("symbol", "") or mint[:8])
                q = str(base.get("symbol", "") or "")
                name = str(quote.get("name", "") or "")
            out.append(CanonicalAsset(
                id=solana_pool_id(mint, dex, pool), provider="solana",
                venue=dex, chain="solana", symbol=sym, name=name, mint=mint,
                quote=q, kind="pool",
                parent_id=f"solana:{mint}"))
        except (ValueError, TypeError, AttributeError):
            continue
    return out
