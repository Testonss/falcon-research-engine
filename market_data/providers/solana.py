"""Solana / DEX live market-data provider (READ-ONLY, Axiom-compatible).

Axiom compatibility is independence, not integration:

  user sees token on Axiom -> copies mint -> falcon live --provider solana
  --mint <MINT> -> Falcon resolves the on-chain market itself via
  documented public infrastructure.

This module never scrapes Axiom, never calls private Axiom endpoints,
never automates trading, never touches wallets, never signs anything.
Data backends are adapters over documented public sources:

- DexScreenerBackend (default): documented public token/pair API, no key.
- SolanaRpcBackend (optional): standard Solana JSON-RPC for token
  decimals/supply enrichment when FALCON_SOLANA_RPC_URL is configured.

Unknown metrics are None (never zero). No market -> explicit error,
never fake pricing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from market_data import Ticker
from market_data.assets import OnChainAsset, OnChainPool, validate_solana_mint
from market_data.health import ConnectionState
from market_data.normalization import MarketLiquidityEvent, parse_float_strict
from market_data.providers.base import LiveMarketDataProvider

logger = logging.getLogger(__name__)

DEXSCREENER_DEFAULT_BASE = "https://api.dexscreener.com"

# Stable, well-known quote mints/symbols on Solana (for display orientation).
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_SYMBOLS = {"USDC", "USDT", "USDH", "UXD", "PAI", "DAI"}


class NoMarketError(RuntimeError):
    """Raised when no safe/usable on-chain market exists for a mint."""


class BackendError(RuntimeError):
    pass


def apply_decimals(raw_amount: Any, decimals: Any) -> Optional[float]:
    """Convert raw token units -> human units. None on bad input.

    A bad decimals value must never silently corrupt a price: out-of-range
    or non-integer decimals return None instead of a wrong number.
    """
    try:
        if decimals is None or isinstance(decimals, bool):
            return None
        d = int(decimals)
        if d < 0 or d > 18:
            return None
        raw = float(raw_amount)  # type: ignore[arg-type]
        import math
        if not math.isfinite(raw) or raw < 0:
            return None
        return raw / (10 ** d)
    except (TypeError, ValueError):
        return None


def _f(x: Any) -> Optional[float]:
    v = parse_float_strict(x)
    if v is None or v <= 0:
        # price-like fields: only positive finite values are real data.
        return v if v is not None and v > 0 else None
    return v


def _f_nonneg(x: Any) -> Optional[float]:
    v = parse_float_strict(x)
    if v is None or v < 0:
        return None
    return v


def _int_or_none(x: Any) -> Optional[int]:
    try:
        if x is None or isinstance(x, bool):
            return None
        v = int(float(x))  # type: ignore[arg-type]
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Pool selection (deterministic, documented)
# --------------------------------------------------------------------------

def select_pool(pairs: List[Dict[str, Any]], mint: str) -> Tuple[Dict[str, Any], str]:
    """Pick the canonical pool for a mint.

    Policy (deterministic, documented):
    1. keep only chainId == 'solana' pairs touching the mint as base or quote;
    2. prefer pairs where the mint is the BASE token (standard orientation);
    3. among the preferred orientation, rank by (liquidity.usd desc,
       volume.h24 desc, pairAddress asc);
    4. the winner is recorded with dex / pool / liquidity / quote.

    Raises NoMarketError when nothing usable exists. Never picks silently
    by list order: ranking keys are explicit and total.
    """
    cands = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        if str(p.get("chainId", "")).lower() != "solana":
            continue
        base = p.get("baseToken", {}) or {}
        quote = p.get("quoteToken", {}) or {}
        base_addr = str(base.get("address", ""))
        quote_addr = str(quote.get("address", ""))
        if base_addr != mint and quote_addr != mint:
            continue
        liq = (((p.get("liquidity") or {}).get("usd")) if isinstance(p.get("liquidity"), dict) else None)
        try:
            liq_v = float(liq) if liq is not None else -1.0
        except (TypeError, ValueError):
            liq_v = -1.0
        import math
        if not math.isfinite(liq_v):
            liq_v = -1.0
        vol24 = (((p.get("volume") or {}).get("h24")) if isinstance(p.get("volume"), dict) else None)
        try:
            vol_v = float(vol24) if vol24 is not None else -1.0
        except (TypeError, ValueError):
            vol_v = -1.0
        if not math.isfinite(vol_v):
            vol_v = -1.0
        cands.append((0 if base_addr == mint else 1, -liq_v, -vol_v,
                      str(p.get("pairAddress", "")), p))
    if not cands:
        raise NoMarketError(f"no solana market found for mint {mint[:8]}...")
    cands.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    winner = cands[0][4]
    orientation = "base" if cands[0][0] == 0 else "quote"
    reason = (f"selected {winner.get('dexId')}/{winner.get('pairAddress')} "
              f"(orientation={orientation}, liquidity_usd={cands[0][1] * -1:.2f}) "
              f"among {len(cands)} solana pools")
    return winner, reason


def normalize_pair(mint: str, pair: Dict[str, Any],
                   now: Optional[datetime] = None) -> MarketLiquidityEvent:
    """Convert one DexScreener pair into a MarketLiquidityEvent.

    Missing metrics -> None. Malformed numerics -> None. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    base = pair.get("baseToken", {}) or {}
    quote = pair.get("quoteToken", {}) or {}
    base_addr = str(base.get("address", ""))
    inverted = (base_addr != mint)  # mint is the quote side
    price_usd = _f(pair.get("priceUsd"))
    price_native = _f(pair.get("priceNative"))
    quote_sym = str(quote.get("symbol", "") or "") or None
    if inverted:
        # API quotes are still expressed per base token; when our mint is the
        # quote side the per-mint price is the reciprocal (when computable).
        quote_sym = str(base.get("symbol", "") or "") or None
        if price_native and price_native > 0:
            price_native = 1.0 / price_native
        else:
            price_native = None
        # price_usd from the API is per base unit too; without a reliable
        # cross rate we must not invent a per-mint USD price.
        if price_usd is not None:
            px_base_usd = price_usd
            price_usd = None
            _ = px_base_usd
    liq = pair.get("liquidity", {}) or {}
    vol = pair.get("volume", {}) or {}
    tx = pair.get("txns", {}) or {}
    m5 = tx.get("m5", {}) or {}
    try:
        mcap = pair.get("marketCap")
        mcap_v = float(mcap) if mcap is not None else None
        import math
        if mcap_v is not None and (not math.isfinite(mcap_v) or mcap_v < 0):
            mcap_v = None
    except (TypeError, ValueError):
        mcap_v = None
    try:
        fdv = pair.get("fdv")
        fdv_v = float(fdv) if fdv is not None else None
        import math
        if fdv_v is not None and (not math.isfinite(fdv_v) or fdv_v < 0):
            fdv_v = None
    except (TypeError, ValueError):
        fdv_v = None
    buys = _int_or_none(m5.get("buys"))
    sells = _int_or_none(m5.get("sells"))
    txns = (buys + sells) if (buys is not None and sells is not None) else None
    return MarketLiquidityEvent(
        provider="solana",
        instrument=f"solana:{mint}",
        event_time=now,
        price_usd=price_usd,
        price_native=price_native,
        quote_symbol=quote_sym,
        liquidity_usd=_f_nonneg(liq.get("usd")) if isinstance(liq, dict) else None,
        market_cap=mcap_v,
        fdv=fdv_v,
        volume_5m=_f_nonneg(vol.get("m5")) if isinstance(vol, dict) else None,
        buys_5m=buys,
        sells_5m=sells,
        txns_5m=txns,
        dex=str(pair.get("dexId", "") or "") or None,
        pool_address=str(pair.get("pairAddress", "") or "") or None,
    )


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

def _redacted(url: str) -> str:
    try:
        u = urlparse(url)
        if u.query:
            return url.split("?")[0] + "?<redacted>"
        return url
    except Exception:
        return "<url>"


def _default_http_get(url: str, timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "falcon-research/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
        if len(body) > 4 * 1024 * 1024:
            raise IOError("response too large (>4MB)")
        return r.status, body


class SolanaDataBackend(ABC):
    @abstractmethod
    async def fetch_pairs(self, mint: str) -> List[Dict[str, Any]]:
        """Return raw pair dicts for a mint (may be empty)."""

    @abstractmethod
    async def fetch_token_meta(self, mint: str) -> Dict[str, Any]:
        """Return {symbol, name, decimals} where known (values may be None)."""


class DexScreenerBackend(SolanaDataBackend):
    """Documented public DexScreener token API. No key required."""

    def __init__(self, base_url: Optional[str] = None,
                 http_timeout: float = 10.0,
                 http_get: Optional[Callable[[str, float], Tuple[int, str]]] = None):
        self.base_url = (base_url or os.getenv("FALCON_DEXSCREENER_BASE")
                         or DEXSCREENER_DEFAULT_BASE).rstrip("/")
        self.http_timeout = float(http_timeout)
        self._http_get = http_get or _default_http_get

    def _url(self, mint: str) -> str:
        return f"{self.base_url}/latest/dex/tokens/{urllib.parse.quote(mint, safe='')}"

    async def fetch_pairs(self, mint: str) -> List[Dict[str, Any]]:
        url = self._url(mint)
        try:
            status, body = await asyncio.to_thread(self._http_get, url, self.http_timeout)
        except Exception as e:
            raise BackendError(f"dexscreener request failed: {e}")
        if status != 200:
            raise BackendError(f"dexscreener HTTP {status} for {_redacted(url)}")
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise BackendError(f"dexscreener malformed JSON: {e}")
        if not isinstance(payload, dict):
            raise BackendError("dexscreener malformed envelope")
        pairs = payload.get("pairs", [])
        if pairs is None:
            return []
        if not isinstance(pairs, list):
            raise BackendError("dexscreener malformed pairs")
        return [p for p in pairs if isinstance(p, dict)]

    async def fetch_token_meta(self, mint: str) -> Dict[str, Any]:
        try:
            pairs = await self.fetch_pairs(mint)
        except BackendError:
            return {"symbol": None, "name": None, "decimals": None}
        for p in pairs:
            for side in ("baseToken", "quoteToken"):
                tok = p.get(side, {}) or {}
                if str(tok.get("address", "")) == mint:
                    return {"symbol": tok.get("symbol"), "name": tok.get("name"),
                            "decimals": None}
        return {"symbol": None, "name": None, "decimals": None}


class SolanaRpcBackend(SolanaDataBackend):
    """Standard Solana JSON-RPC (getAccountInfo) for decimals enrichment.

    Optional. Only used when FALCON_SOLANA_RPC_URL is configured. Wraps
    another backend for pairs and adds decimals where parseable.
    """

    def __init__(self, inner: SolanaDataBackend,
                 rpc_url: Optional[str] = None,
                 http_timeout: float = 10.0,
                 http_get: Optional[Callable[[str, float], Tuple[int, str]]] = None):
        self.inner = inner
        self.rpc_url = rpc_url or os.getenv("FALCON_SOLANA_RPC_URL", "")
        self.http_timeout = float(http_timeout)
        self._http_get = http_get or _default_http_get

    async def fetch_pairs(self, mint: str) -> List[Dict[str, Any]]:
        return await self.inner.fetch_pairs(mint)

    async def fetch_token_meta(self, mint: str) -> Dict[str, Any]:
        meta = await self.inner.fetch_token_meta(mint)
        if meta.get("decimals") is not None or not self.rpc_url:
            return meta
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                               "params": [mint, {"encoding": "jsonParsed"}]})
            status, text = await asyncio.to_thread(
                _rpc_post, self.rpc_url, body, self.http_timeout)
            if status != 200:
                return meta
            payload = json.loads(text)
            data = ((payload.get("result") or {}).get("value") or {}).get("data") or {}
            parsed = (data.get("parsed") or {}).get("info") or {}
            dec = parsed.get("decimals")
            if isinstance(dec, int) and 0 <= dec <= 18:
                meta["decimals"] = dec
        except Exception as e:
            logger.debug("solana rpc decimals enrichment failed: %s", e)
        return meta


def _rpc_post(url: str, body: str, timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body.encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "falcon-research/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8")
        if len(text) > 1024 * 1024:
            raise IOError("rpc response too large")
        return r.status, text


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------

class SolanaDexMarketDataProvider(LiveMarketDataProvider):
    """Polling live provider for one Solana mint. Public data only."""

    provider_name = "solana"

    def __init__(self, mint: str, poll_interval_s: float = 15.0,
                 stale_after_s: float = 60.0,
                 backend: Optional[SolanaDataBackend] = None,
                 http_timeout: float = 10.0,
                 max_seen_ids: int = 20000,
                 now_fn: Optional[Callable[[], float]] = None):
        super().__init__(stale_after_s=stale_after_s, max_seen_ids=max_seen_ids,
                         now_fn=now_fn)
        self.mint = validate_solana_mint(mint)
        self.asset = OnChainAsset(chain="solana", mint=self.mint)
        self.poll_interval = max(2.0, float(poll_interval_s))
        self._backend = backend or DexScreenerBackend(http_timeout=http_timeout)
        helius = os.getenv("FALCON_HELIUS_API_KEY", "")
        if helius and isinstance(self._backend, DexScreenerBackend):
            logger.debug("helius key configured; reserved for optional enhanced backend")
        self._connect_lock = asyncio.Lock()
        self._pool: Optional[OnChainPool] = None
        self._selection_reason = ""
        self._last_event: Optional[MarketLiquidityEvent] = None
        self._symbol: Optional[str] = None
        self._name: Optional[str] = None
        self._decimals: Optional[int] = None
        self._failures = 0

    # -- resolution -----------------------------------------------------------
    async def resolve(self) -> Dict[str, Any]:
        """Resolve token metadata + canonical pool. Raises explicit errors."""
        pairs = await self._backend.fetch_pairs(self.mint)
        selected, reason = select_pool(pairs, self.mint)
        meta = await self._backend.fetch_token_meta(self.mint)
        base = selected.get("baseToken", {}) or {}
        quote = selected.get("quoteToken", {}) or {}
        if str(base.get("address", "")) == self.mint:
            self._symbol = base.get("symbol") or meta.get("symbol")
            self._name = base.get("name") or meta.get("name")
            quote_mint = str(quote.get("address", ""))
            quote_sym = str(quote.get("symbol", "") or "")
        else:
            self._symbol = quote.get("symbol") or meta.get("symbol")
            self._name = quote.get("name") or meta.get("name")
            quote_mint = str(base.get("address", ""))
            quote_sym = str(base.get("symbol", "") or "")
        if isinstance(meta.get("decimals"), int):
            self._decimals = meta["decimals"]
        self.asset = OnChainAsset(chain="solana", mint=self.mint,
                                  symbol=self._symbol, name=self._name,
                                  decimals=self._decimals)
        self._pool = OnChainPool(chain="solana",
                                 dex=str(selected.get("dexId", "") or "unknown"),
                                 pool_address=str(selected.get("pairAddress", "")),
                                 base_mint=self.mint, quote_mint=quote_mint,
                                 quote_symbol=quote_sym or None)
        self._selection_reason = reason
        logger.info("solana resolved %s -> %s", self.mint[:8], reason)
        return {"asset": self.asset, "pool": self._pool, "reason": reason}

    async def refresh_once(self) -> Optional[MarketLiquidityEvent]:
        """Single poll: resolve if needed, normalize, dedup, order-guard."""
        try:
            pairs = await self._backend.fetch_pairs(self.mint)
        except BackendError as e:
            self._note_rejected()
            self._failures += 1
            logger.warning("solana backend failure (%d): %s", self._failures, e)
            return None
        except Exception as e:
            self._note_rejected()
            self._failures += 1
            logger.warning("solana provider failure (%d): %s", self._failures, e)
            return None
        try:
            selected, reason = select_pool(pairs, self.mint)
        except NoMarketError as e:
            self._note_rejected()
            logger.warning("solana no market: %s", e)
            return None
        # Re-resolve pool deterministically each poll (winner may change with
        # liquidity migration); the policy + reason stay inspectable.
        base = selected.get("baseToken", {}) or {}
        if str(base.get("address", "")) == self.mint:
            self._symbol = base.get("symbol") or self._symbol
            self._name = base.get("name") or self._name
        self._pool = OnChainPool(
            chain="solana", dex=str(selected.get("dexId", "") or "unknown"),
            pool_address=str(selected.get("pairAddress", "")),
            base_mint=self.mint,
            quote_mint=str((selected.get("quoteToken", {}) or {}).get("address", "")),
            quote_symbol=str((selected.get("quoteToken", {}) or {}).get("symbol", "") or "") or None)
        self._selection_reason = reason
        event = normalize_pair(self.mint, selected)
        key = self._ordering_key("liquidity", f"solana:{self.mint}")
        if self._is_out_of_order(key, event.event_time):
            return None
        # Dedup identical polls: same content hash -> accepted once.
        content = "|".join(str(x) for x in (
            event.price_usd, event.liquidity_usd, event.volume_5m,
            event.buys_5m, event.sells_5m, event.pool_address))
        import hashlib
        event.event_id = "solana:liq:" + hashlib.sha256(
            f"{self.mint}|{content}".encode()).hexdigest()[:24]
        if self._is_duplicate(event.event_id):
            # Still counts as a live message (feed is active) without
            # duplicating downstream state.
            self._sm.mark_message()
            return None
        self._last_event = event
        self._failures = 0
        self._note_accepted()
        return event

    # -- lifecycle ------------------------------------------------------------------
    async def connect(self, max_retries: int = 3):
        async with self._connect_lock:
            if self._sm.state in (ConnectionState.CONNECTED, ConnectionState.STALE):
                return
            if self._sm.state == ConnectionState.STOPPED:
                from market_data.health import ConnectionStateMachine
                self._sm = ConnectionStateMachine(
                    self.provider_name, stale_after_s=self._sm.stale_after_s)
            self._sm.transition(ConnectionState.CONNECTING)
            last_err: Optional[Exception] = None
            for attempt in range(1, max(1, max_retries) + 1):
                try:
                    await self.resolve()
                    break
                except (BackendError, NoMarketError, OSError) as e:
                    last_err = e
                    if attempt >= max_retries:
                        self._sm.transition(ConnectionState.DISCONNECTED)
                        raise
                    await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 8.0))
            self._sm.mark_connected()
            if self._sm.state != ConnectionState.CONNECTED:
                self._sm.transition(ConnectionState.CONNECTED)
            logger.info("solana live connected: %s (%s)", self.mint[:8], self._symbol)
            try:
                await self.refresh_once()  # first reading now, not after one poll interval
            except Exception as e:
                logger.debug("solana initial refresh deferred: %s", e)
            await self._ensure_reader(self._poll_loop)

    async def disconnect(self):
        if self._sm.state == ConnectionState.STOPPED:
            return
        self._sm.transition(ConnectionState.STOPPING)
        await self._stop_reader()
        self._sm.transition(ConnectionState.STOPPED)

    async def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("solana poll error (continuing): %s", e)
            if not await self._sleep_cancellable(self.poll_interval):
                return

    # -- MarketDataSource surface ---------------------------------------------------------
    async def get_ticker(self, symbol: str = "") -> Optional[Ticker]:
        if self._last_event and self._last_event.price_usd:
            return Ticker(symbol=self._symbol or self.mint[:8],
                          price=self._last_event.price_usd, volume=0.0,
                          timestamp=self._last_event.event_time,
                          event_id=self._last_event.event_id)
        return None  # honest: no price observed yet -> None, never zero

    async def get_candles(self, symbol: str = "", interval: str = "",
                          limit: int = 100) -> list:
        # Documented limitation: the DexScreener token API exposes price +
        # volume snapshots, not OHLCV candle history. Return [] honestly
        # rather than synthesizing bars.
        return []

    async def get_order_book(self, symbol: str = "", depth: int = 10):
        # No L2 book exists for this backend; best-book only via liquidity
        # event. Return None explicitly (no fake depth).
        return None

    def get_health(self) -> Dict[str, Any]:
        self._sm.refresh_stale()
        h = self._sm.health().to_dict()
        h["mint"] = self.mint
        h["symbol"] = self._symbol
        h["dex"] = self._pool.dex if self._pool else None
        h["pool"] = self._pool.pool_address if self._pool else None
        return h

    def describe(self) -> Dict[str, Any]:
        return {"provider": "solana", "mint": self.mint, "symbol": self._symbol,
                "dex": self._pool.dex if self._pool else None,
                "pool": self._pool.pool_address if self._pool else None,
                "selection": self._selection_reason,
                "credentials_required": False,
                "optional_env": ["FALCON_SOLANA_RPC_URL", "FALCON_HELIUS_API_KEY"]}
