"""Canonical asset registry: the single source of truth for market state.

Every asset/market has ONE canonical identity:

  binance:spot:BTCUSDT            (CEX spot instrument)
  solana:<mint>                   (on-chain asset, canonical pool)
  solana:<mint>:<dex>:<pool>      (one specific on-chain pool/market)

ASSET LEVEL (symbol, name, chain, mint, aggregate price/mcap/fdv) is never
mixed with MARKET/POOL LEVEL (venue, pool address, quote, liquidity,
pool volume, buys/sells, bid/ask). Pool rows carry the pool fields;
asset rows carry the aggregates.

Each accepted update bumps a monotonic revision. Consumers read one
coherent revision per render (Radar, Selected, Chart, future engines all
see the same state). Deltas (`changed_since`) let the UI touch only rows
that actually moved. Duplicate/out-of-order updates are rejected where
semantics allow; delayed data never rolls state backward.

Pure Python: no Qt, no network, no provider imports.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple


BINANCE_QUOTES = ("USDT", "USDC", "BTC", "ETH", "BNB", "FDUSD")


def binance_id(symbol: str) -> str:
    """Canonical id for a Binance spot symbol (accepts BTCUSDT or BTC/USDT)."""
    s = (symbol or "").strip().upper().replace("/", "")
    if not s:
        raise ValueError(f"invalid binance symbol: {symbol!r}")
    return f"binance:spot:{s}"


def solana_mint_id(mint: str) -> str:
    """Canonical id for a Solana token (mint is authoritative)."""
    m = (mint or "").strip()
    if not m:
        raise ValueError(f"invalid solana mint: {mint!r}")
    return f"solana:{m}"


def solana_pool_id(mint: str, dex: str, pool: str) -> str:
    """Canonical id for one specific on-chain pool/market."""
    m, d, p = (mint or "").strip(), (dex or "").strip(), (pool or "").strip()
    if not m or not d or not p:
        raise ValueError(f"invalid solana pool identity: {(mint, dex, pool)!r}")
    return f"solana:{m}:{d}:{p}"


def mt5_id(server: str, symbol: str) -> str:
    """Canonical id for an MT5 broker instrument.

    Form: ``mt5:<server>:<exact_symbol>`` — e.g.
    ``mt5:MetaQuotes-Demo:EURUSD`` or ``mt5:Broker-Demo:EURUSD.a``.

    Broker suffixes (``.a``, ``.m``, ``_ECN`` …) are significant and never
    stripped: different suffixed symbols are different instruments.
    Server and symbol are stored verbatim except surrounding whitespace.
    Neither may be empty nor contain ':'.
    """
    srv = (server or "").strip()
    sym = (symbol or "").strip()
    if not srv or not sym:
        raise ValueError(f"invalid mt5 identity: {(server, symbol)!r}")
    if ":" in srv or ":" in sym:
        raise ValueError(f"invalid mt5 identity (':' not allowed): {(server, symbol)!r}")
    return f"mt5:{srv}:{sym}"


def parse_mt5_id(cid: str) -> Dict[str, Optional[str]]:
    """Split an ``mt5:<server>:<symbol>`` id. Raises ValueError otherwise."""
    if not isinstance(cid, str) or not cid.startswith("mt5:"):
        raise ValueError(f"invalid mt5 canonical id: {cid!r}")
    parts = cid.split(":")
    if len(parts) != 3 or not all(p for p in parts):
        raise ValueError(f"invalid mt5 canonical id: {cid!r}")
    return {"provider": "mt5", "server": parts[1], "symbol": parts[2]}


def parse_canonical_id(cid: str) -> Dict[str, Optional[str]]:
    """Split a canonical id into {provider, venue, ref, pool}.

    Raises ValueError on malformed ids. Never guesses.
    """
    if not isinstance(cid, str) or not cid:
        raise ValueError(f"invalid canonical id: {cid!r}")
    parts = cid.split(":")
    if len(parts) < 2 or not all(parts):
        raise ValueError(f"invalid canonical id: {cid!r}")
    provider = parts[0]
    if provider == "binance" and len(parts) == 3 and parts[1] == "spot":
        return {"provider": "binance", "venue": "spot", "ref": parts[2], "pool": None}
    if provider == "solana" and len(parts) in (2, 4):
        if len(parts) == 2:
            return {"provider": "solana", "venue": "", "ref": parts[1], "pool": None}
        return {"provider": "solana", "venue": parts[2], "ref": parts[1],
                "pool": parts[3]}
    if provider == "mt5" and len(parts) == 3:
        # mt5:<server>:<exact_symbol> — server is the venue context.
        return {"provider": "mt5", "venue": parts[1], "ref": parts[2],
                "pool": None, "server": parts[1], "symbol": parts[2]}
    raise ValueError(f"unsupported canonical id: {cid!r}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CanonicalAsset:
    """Identity only. No prices, no liveness here."""
    id: str
    provider: str            # "binance" | "solana" | ...
    venue: str               # "spot" | dex id | ""
    chain: str               # "" for CEX, "solana", ...
    symbol: str              # display: "BTC/USDT" | token symbol
    name: str = ""           # display only
    mint: Optional[str] = None
    quote: str = ""          # "USDT" | quote symbol
    kind: str = "asset"      # "asset" | "pool"
    parent_id: Optional[str] = None  # pool -> asset id
    supported: Tuple[str, ...] = ()  # chart timeframes honestly servable
    # MT5 symbol metadata (None/0 = unknown; provider owns, UI only consumes).
    digits: Optional[int] = None
    point: Optional[float] = None
    contract_size: Optional[float] = None
    volume_min: Optional[float] = None
    volume_max: Optional[float] = None
    volume_step: Optional[float] = None
    currency_base: str = ""
    currency_profit: str = ""


@dataclass
class AssetLiveState:
    """Mutable live state for ONE canonical id. Owned by the registry."""
    revision: int = 0
    # market data (None = unknown, never zero-by-default)
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_bps: Optional[float] = None
    change_24h: Optional[float] = None
    volume_24h: Optional[float] = None       # quote units (CEX 24h)
    liquidity_usd: Optional[float] = None   # pool level
    market_cap: Optional[float] = None      # asset level
    fdv: Optional[float] = None
    volume_5m: Optional[float] = None       # pool level (USD)
    buys_5m: Optional[int] = None
    sells_5m: Optional[int] = None
    dex: Optional[str] = None
    pool_address: Optional[str] = None
    # live candle (single active view per asset; history lives in the cache)
    candle_open: Optional[float] = None
    candle_high: Optional[float] = None
    candle_low: Optional[float] = None
    candle_close: Optional[float] = None
    candle_volume: Optional[float] = None
    candle_interval: str = ""
    candle_closed: Optional[bool] = None
    # feed health
    state: str = "CONNECTING"
    connected: bool = False
    stale: bool = False
    messages: int = 0
    error: Optional[str] = None
    source: str = ""
    observed_at: Optional[str] = None
    age_s: Optional[float] = None
    # ordering / dedupe guards (per key)
    last_event_time: Optional[float] = None  # epoch seconds
    last_event_id: Optional[str] = None
    event_time: Optional[datetime] = None
    recv_time: Optional[datetime] = None


@dataclass
class _HistoryEntry:
    bars: Tuple[tuple, ...] = ()      # (iso, o, h, l, c, v, closed)
    fetched_at: float = 0.0           # monotonic seconds
    complete: bool = False


class AssetRegistry:
    """Authoritative id -> (identity, live state) map with revisions."""

    # Memory bounds: leaving Falcon open for hours must not grow unbounded.
    MAX_SEEN_IDS = 20000
    MAX_HISTORY_ENTRIES = 600
    MAX_HISTORY_BARS = 500

    def __init__(self):
        self._assets: Dict[str, CanonicalAsset] = {}
        self._states: Dict[str, AssetLiveState] = {}
        self._order: List[str] = []          # deterministic insertion order
        self._rev = 0                        # global revision clock
        self._seen_ids: Set[str] = set()     # cross-key event-id dedupe
        self._seen_order: List[str] = []      # FIFO eviction for _seen_ids
        self._favorites: Set[str] = set()
        self._history: Dict[Tuple[str, str], _HistoryEntry] = {}
        self._dirty: Set[str] = set()        # content-changed ids awaiting publish
        # Normalized search index: id -> "symbol name mint" (lowercased once).
        self._search_hay: Dict[str, str] = {}
        # Internal cache diagnostics (never shown as developer noise in UI).
        self._hist_hits = 0
        self._hist_misses = 0

    # -- identity ----------------------------------------------------------
    def register(self, asset: CanonicalAsset, *, strict: bool = True) -> bool:
        """Register identity. Returns True when newly added.

        strict=True (default) fails fast on malformed canonical ids.
        strict=False passes keys through verbatim for the injected-provider
        test seam, where the caller owns key semantics.
        """
        if asset.id in self._assets:
            return False
        if strict:
            parse_canonical_id(asset.id)  # fail fast on malformed ids
        self._assets[asset.id] = asset
        self._states[asset.id] = AssetLiveState()
        self._order.append(asset.id)
        self._search_hay[asset.id] = (
            f"{asset.symbol} {asset.name} {asset.mint or ''}".lower())
        return True

    def enrich_identity(self, cid: str, placeholders: Tuple[str, ...] = (),
                        **fields) -> bool:
        """Fill in blank identity fields (symbol/name/mint/quote/chain/venue).

        Never overwrites populated fields; values currently holding a
        placeholder (e.g. the raw registry key used before first data)
        count as blank. The canonical id itself is immutable.
        MT5 metadata fields (digits/point/contract_size/volume_*/currency_*)
        are set when supplied and valid (digits 0..10, positive numerics).
        Returns True when anything changed.
        """
        asset = self._assets.get(cid)
        if asset is None:
            return False
        patch = {}
        for field in ("symbol", "name", "mint", "quote", "chain", "venue"):
            value = fields.get(field)
            current = getattr(asset, field)
            if value and (not current or current in placeholders):
                patch[field] = value
        supported = fields.get("supported")
        if supported is not None and not asset.supported:
            try:
                patch["supported"] = tuple(supported)
            except TypeError:
                pass
        # MT5 symbol metadata: set when supplied and valid; may overwrite
        # unknown (None) but never clobbers known values with unknown.
        if "digits" in fields:
            try:
                d = fields.get("digits")
                if d is not None:
                    di = int(d)
                    if 0 <= di <= 10 and asset.digits in (None, 0):
                        patch["digits"] = di
                    elif 0 <= di <= 10 and asset.digits != di:
                        # Broker metadata is authoritative: accept updates.
                        patch["digits"] = di
            except (TypeError, ValueError):
                pass
        for num_field in ("point", "contract_size", "volume_min",
                          "volume_max", "volume_step"):
            if num_field in fields:
                try:
                    v = fields.get(num_field)
                    if v is None:
                        continue
                    f = float(v)
                    import math as _m
                    if not _m.isfinite(f) or f < 0:
                        continue
                    cur = getattr(asset, num_field)
                    if cur in (None, 0) or cur != f:
                        patch[num_field] = f
                except (TypeError, ValueError):
                    pass
        for s_field in ("currency_base", "currency_profit"):
            if s_field in fields:
                v = fields.get(s_field)
                if isinstance(v, str) and v and not getattr(asset, s_field):
                    patch[s_field] = v
                elif isinstance(v, str) and v and getattr(asset, s_field) != v:
                    patch[s_field] = v
        if not patch:
            return False
        self._assets[cid] = dataclasses.replace(asset, **patch)
        updated = self._assets[cid]
        self._search_hay[cid] = (
            f"{updated.symbol} {updated.name} {updated.mint or ''}".lower())
        return True

    def get(self, cid: str) -> Optional[CanonicalAsset]:
        return self._assets.get(cid)

    def state(self, cid: str) -> Optional[AssetLiveState]:
        return self._states.get(cid)

    def ids(self) -> List[str]:
        return list(self._order)

    def __len__(self) -> int:
        return len(self._assets)

    @property
    def revision(self) -> int:
        return self._rev

    # -- updates ------------------------------------------------------------
    def _accept(self, cid: str, event_time: Optional[datetime],
                event_id: Optional[str]) -> Tuple[Optional[AssetLiveState], bool]:
        """Ordering + dedupe gate. Returns (state, accepted).

        Acceptance alone does NOT advance the revision: the revision is a
        content clock (bumped only when an apply actually changes stored
        state), so every consumer reading the same revision sees the same
        values. Accepted-but-identical updates refresh liveness timestamps
        without faking movement.
        """
        st = self._states.get(cid)
        if st is None:
            return None, False
        if event_id:
            if event_id in self._seen_ids or event_id == st.last_event_id:
                return st, False
        epoch: Optional[float] = None
        if event_time is not None:
            try:
                epoch = event_time.timestamp()
            except Exception:
                epoch = None
        if epoch is not None and st.last_event_time is not None \
                and epoch < st.last_event_time:
            return st, False  # delayed data must not roll state backward
        if event_id:
            self._remember_seen(event_id)
            st.last_event_id = event_id
        if epoch is not None and (st.last_event_time is None
                                  or epoch > st.last_event_time):
            st.last_event_time = epoch
        st.event_time = event_time
        st.recv_time = _utcnow()
        return st, True

    def _remember_seen(self, event_id: str) -> None:
        """Bounded event-id dedupe (FIFO eviction keeps memory flat)."""
        if event_id in self._seen_ids:
            return
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        overflow = len(self._seen_order) - self.MAX_SEEN_IDS
        if overflow > 0:
            for old in self._seen_order[:overflow]:
                self._seen_ids.discard(old)
            del self._seen_order[:overflow]

    def _bump(self, st: AssetLiveState, cid: str) -> None:
        self._rev += 1
        st.revision = self._rev
        self._dirty.add(cid)

    def apply_quote(self, cid: str, *, price: Optional[float] = None,
                    change_24h: Optional[float] = None,
                    volume_24h: Optional[float] = None,
                    event_time: Optional[datetime] = None,
                    event_id: Optional[str] = None) -> Tuple[bool, bool]:
        """Returns (accepted, changed). None fields are skipped, not cleared."""
        st, ok = self._accept(cid, event_time, event_id)
        if not ok or st is None:
            return False, False
        changed = False
        if price is not None and price != st.price:
            st.price = price
            changed = True
        if change_24h is not None and change_24h != st.change_24h:
            st.change_24h = change_24h
            changed = True
        if volume_24h is not None and volume_24h != st.volume_24h:
            st.volume_24h = volume_24h
            changed = True
        if changed:
            self._bump(st, cid)
        return True, changed

    def apply_book(self, cid: str, *, bid: Optional[float] = None,
                   ask: Optional[float] = None,
                   event_time: Optional[datetime] = None,
                   event_id: Optional[str] = None) -> Tuple[bool, bool]:
        st, ok = self._accept(cid, event_time, event_id)
        if not ok or st is None:
            return False, False
        changed = False
        if bid is not None and bid != st.bid:
            st.bid = bid
            changed = True
        if ask is not None and ask != st.ask:
            st.ask = ask
            changed = True
        if st.bid and st.ask and st.bid > 0 and st.ask >= st.bid:
            spread = (st.ask - st.bid) / st.bid * 10000.0
            if spread != st.spread_bps:
                st.spread_bps = spread
                changed = True
        if changed:
            self._bump(st, cid)
        return True, changed

    def apply_liquidity(self, cid: str, *,
                        liquidity_usd: Optional[float] = None,
                        market_cap: Optional[float] = None,
                        fdv: Optional[float] = None,
                        volume_5m: Optional[float] = None,
                        buys_5m: Optional[int] = None,
                        sells_5m: Optional[int] = None,
                        dex: Optional[str] = None,
                        pool_address: Optional[str] = None,
                        price_usd: Optional[float] = None,
                        price_native: Optional[float] = None,
                        quote_symbol: Optional[str] = None,
                        event_time: Optional[datetime] = None,
                        event_id: Optional[str] = None) -> Tuple[bool, bool]:
        st, ok = self._accept(cid, event_time, event_id)
        if not ok or st is None:
            return False, False
        changed = False
        for field, value in (
                ("liquidity_usd", liquidity_usd), ("market_cap", market_cap),
                ("fdv", fdv), ("volume_5m", volume_5m), ("buys_5m", buys_5m),
                ("sells_5m", sells_5m), ("dex", dex),
                ("pool_address", pool_address)):
            if value is not None and getattr(st, field) != value:
                setattr(st, field, value)
                changed = True
        if price_usd is not None and price_usd != st.price:
            st.price = price_usd
            changed = True
        if changed:
            self._bump(st, cid)
        return True, changed

    def apply_candle(self, cid: str, *,
                     open: Optional[float] = None,  # noqa: A002
                     high: Optional[float] = None,
                     low: Optional[float] = None,
                     close: Optional[float] = None,
                     volume: Optional[float] = None,
                     interval: str = "",
                     is_closed: Optional[bool] = None,
                     event_time: Optional[datetime] = None,
                     event_id: Optional[str] = None) -> Tuple[bool, bool]:
        st, ok = self._accept(cid, event_time, event_id)
        if not ok or st is None:
            return False, False
        changed = False
        for field, value in (
                ("candle_open", open), ("candle_high", high),
                ("candle_low", low), ("candle_close", close),
                ("candle_volume", volume),
                ("candle_closed", is_closed)):
            if value is not None and getattr(st, field) != value:
                setattr(st, field, value)
                changed = True
        if interval and interval != st.candle_interval:
            st.candle_interval = interval
            changed = True
        if changed:
            self._bump(st, cid)
        return True, changed

    def apply_health(self, cid: str, *, state: str = "",
                     connected: Optional[bool] = None,
                     stale: Optional[bool] = None,
                     messages: Optional[int] = None,
                     error: Optional[str] = None,
                     source: str = "",
                     observed_at: Optional[str] = None,
                     age_s: Optional[float] = None,
                     dex: Optional[str] = None,
                     pool_address: Optional[str] = None) -> bool:
        st = self._states.get(cid)
        if st is None:
            return False
        material = False
        if state and state != st.state:
            st.state = state
            material = True
        for field, value in (("connected", connected), ("stale", stale),
                             ("dex", dex), ("pool_address", pool_address)):
            if value is None:
                continue
            if getattr(st, field) != value:
                setattr(st, field, value)
                material = True
        if messages is not None:
            st.messages = messages
        if st.error != error:
            st.error = error
            material = True
        if source:
            st.source = source
        st.observed_at = observed_at
        st.age_s = age_s
        if material:
            self._rev += 1
            st.revision = self._rev
            self._dirty.add(cid)
        return True

    def mark_changed(self, cid: str) -> bool:
        """Flag an id dirty (e.g. chart cache updated) with a revision bump."""
        st = self._states.get(cid)
        if st is None:
            return False
        self._bump(st, cid)
        return True

    # -- deltas --------------------------------------------------------------
    def changed_since(self, rev: int) -> List[str]:
        """Ids whose state revision is newer than rev, in registry order."""
        return [cid for cid in self._order
                if self._states[cid].revision > rev]

    def drain_dirty(self) -> List[str]:
        """Content-changed ids since last drain, in registry order."""
        out = [cid for cid in self._order if cid in self._dirty]
        self._dirty.clear()
        return out

    # -- search / filter -------------------------------------------------------
    def search_hay(self, cid: str) -> str:
        """Pre-normalized searchable string (computed once, not per render)."""
        hit = self._search_hay.get(cid)
        if hit is not None:
            return hit
        a = self._assets.get(cid)
        if a is None:
            return ""
        hay = f"{a.symbol} {a.name} {a.mint or ''}".lower()
        self._search_hay[cid] = hay
        return hay

    def search(self, query: str) -> List[str]:
        """Substring match over symbol, name and mint (case-insensitive)."""
        q = (query or "").strip().lower()
        if not q:
            return list(self._order)
        return [cid for cid in self._order
                if q in self.search_hay(cid)]

    def filter(self, *, provider: str = "", chain: str = "",
               quote: str = "", favorites_only: bool = False) -> List[str]:
        """Metadata filter. Empty string = no constraint on that axis."""
        out = []
        for cid in self._order:
            a = self._assets[cid]
            if provider and a.provider != provider:
                continue
            if chain and a.chain != chain:
                continue
            if quote and a.quote != quote:
                continue
            if favorites_only and cid not in self._favorites:
                continue
            out.append(cid)
        return out

    def providers(self) -> List[str]:
        seen: List[str] = []
        for cid in self._order:
            p = self._assets[cid].provider
            if p not in seen:
                seen.append(p)
        return seen

    def chains(self) -> List[str]:
        seen: List[str] = []
        for cid in self._order:
            c = self._assets[cid].chain
            if c not in seen:
                seen.append(c)
        return seen

    def quotes(self) -> List[str]:
        seen: List[str] = []
        for cid in self._order:
            q = self._assets[cid].quote
            if q and q not in seen:
                seen.append(q)
        return seen

    # -- favorites (in-memory flags; persistence lives in store.py) ------------
    def set_favorite(self, cid: str, favorite: bool = True) -> bool:
        if cid not in self._assets:
            return False
        if favorite:
            self._favorites.add(cid)
        else:
            self._favorites.discard(cid)
        return True

    def is_favorite(self, cid: str) -> bool:
        return cid in self._favorites

    def favorites(self) -> List[str]:
        return [cid for cid in self._order if cid in self._favorites]

    # -- history cache -----------------------------------------------------------
    def history_put(self, cid: str, timeframe: str,
                    bars: Tuple[tuple, ...]) -> None:
        bounded = tuple(bars[-self.MAX_HISTORY_BARS:])
        self._history[(cid, timeframe)] = _HistoryEntry(
            bars=bounded, fetched_at=time.monotonic(), complete=True)
        if len(self._history) > self.MAX_HISTORY_ENTRIES:
            # Evict the stalest entry (oldest fetch) to stay bounded.
            oldest = min(self._history,
                         key=lambda k: self._history[k].fetched_at)
            self._history.pop(oldest, None)

    def history_get(self, cid: str, timeframe: str) -> Tuple[tuple, ...]:
        entry = self._history.get((cid, timeframe))
        return entry.bars if entry else ()

    def history_fresh(self, cid: str, timeframe: str, ttl_s: float) -> bool:
        entry = self._history.get((cid, timeframe))
        if not entry or not entry.bars:
            self._hist_misses += 1
            return False
        try:
            fresh = (time.monotonic() - entry.fetched_at) < float(ttl_s)
        except (TypeError, ValueError):
            self._hist_misses += 1
            return False
        if fresh:
            self._hist_hits += 1
        else:
            self._hist_misses += 1
        return fresh

    def history_age_s(self, cid: str, timeframe: str) -> Optional[float]:
        """Seconds since the cached bars were fetched (None when empty)."""
        entry = self._history.get((cid, timeframe))
        if not entry or not entry.bars:
            return None
        return max(0.0, time.monotonic() - entry.fetched_at)

    def history_stats(self) -> Dict[str, int]:
        """Internal cache diagnostics: hits/misses/entries (not UI noise)."""
        return {"hits": self._hist_hits, "misses": self._hist_misses,
                "entries": len(self._history)}

    def priority_order(self, selected: str = "",
                       recent: Tuple[str, ...] = ()) -> List[str]:
        """Startup/refresh priority: selected, favorites, recent, rest."""
        favs = set(self._favorites)
        recent_set = set(recent or ())
        ranked = []
        for cid in self._order:
            if cid == selected:
                rank = 0
            elif cid in favs:
                rank = 1
            elif cid in recent_set:
                rank = 2
            else:
                rank = 3
            ranked.append((rank, cid))
        ranked.sort(key=lambda t: (t[0], self._order.index(t[1])))
        return [cid for _, cid in ranked]
