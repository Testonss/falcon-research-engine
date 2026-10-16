"""Canonical display formatting for Falcon Terminal (no Qt/Tk dependency).

Contract (P0):
- Instrument-aware price precision. MT5 digits/point are authoritative
  when available; otherwise magnitude-aware significant formatting.
- Semantic formatters are distinct: price vs money vs quantity vs
  percent vs lots vs volume vs market-cap vs spread. Never reuse money
  formatting for raw volume or instrument price.
- Never render a valid small price as "0.00".
- All formatters are pure, deterministic, None-safe ("—" when unknown).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


EMDASH = "—"


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        # Reject bools (True -> 1.0 would invent a price).
        if isinstance(value, bool):
            return None
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _norm_digits(digits: Any) -> Optional[int]:
    try:
        if digits is None:
            return None
        if isinstance(digits, bool):
            return None
        d = int(digits)
    except (TypeError, ValueError):
        return None
    if d < 0 or d > 10:
        return None
    return d


def magnitude_digits(value: float) -> int:
    """Decimals for magnitude-aware price display (no instrument metadata)."""
    a = abs(float(value))
    if a == 0:
        return 2
    if a >= 1000:
        return 2
    if a >= 100:
        return 2
    if a >= 10:
        return 3
    if a >= 1:
        return 4
    if a >= 0.1:
        return 5
    if a >= 0.01:
        return 5
    if a >= 0.001:
        return 6
    if a >= 0.0001:
        return 7
    if a >= 0.00001:
        return 8
    return 10


def infer_price_digits(value: Any) -> int:
    v = _f(value)
    if v is None:
        return 2
    return magnitude_digits(v)


def chart_axis_digits(prices: Any = None, instrument_digits: Any = None,
                      lo: Any = None, hi: Any = None) -> int:
    """Digits for chart Y-axis / current-price tag.

    Prefers authoritative instrument digits; otherwise infers from the
    visible range (or the sample prices when no range is given).
    """
    d = _norm_digits(instrument_digits)
    if d is not None:
        return d
    lv = _f(lo)
    hv = _f(hi)
    if lv is not None and hv is not None and math.isfinite(lv) and math.isfinite(hv):
        mid = (abs(lv) + abs(hv)) / 2.0
        span = abs(hv - lv)
        if span > 0 and mid > 0:
            # Tight zoom on a large price needs more decimals
            # (e.g. 81570..81575 needs cents; 1.1216..1.1219 needs 5).
            try:
                import math as _m
                need = max(0, -int(_m.floor(_m.log10(span / 200.0))))
                mag = magnitude_digits(mid)
                return max(2, min(10, max(mag, min(need, 10))))
            except (ValueError, OverflowError):
                return magnitude_digits(mid)
        if mid > 0:
            return magnitude_digits(mid)
    try:
        seq = list(prices or [])
    except TypeError:
        seq = []
    vals = [_f(x) for x in seq]
    vals = [x for x in vals if x is not None and x != 0]
    if vals:
        typical = sorted(abs(x) for x in vals)[len(vals) // 2]
        return magnitude_digits(typical)
    return 2


def format_price(value: Any, digits: Any = None) -> str:
    """Instrument price: digits when known, else magnitude-aware.

    EURUSD digits=5 -> "1.12184"; USDJPY digits=3 -> "149.327";
    BTC auto -> "81,572.63"; DOGE auto -> "0.08110";
    memecoin auto -> "0.00001234". Never "0.00" for valid small prices.
    """
    v = _f(value)
    if v is None:
        return EMDASH
    d = _norm_digits(digits)
    if d is None:
        d = magnitude_digits(v)
    try:
        return f"{v:,.{d}f}"
    except (ValueError, OverflowError):
        return EMDASH


def format_money(value: Any, currency: str = "") -> str:
    """Account money (balance/equity/margin): always 2 decimals with grouping."""
    v = _f(value)
    if v is None:
        return EMDASH
    sign = "-" if v < 0 else ""
    cur = (currency or "").strip()
    prefix = f"{cur} " if cur else "$"
    # Use $ prefix by default; currency code prefix when supplied.
    if cur and cur != "$":
        return f"{sign}{cur} {abs(v):,.2f}"
    return f"{sign}${abs(v):,.2f}"


def format_signed_money(value: Any, currency: str = "") -> str:
    """Floating P/L with explicit sign: +$0.15 / -$1.20."""
    v = _f(value)
    if v is None:
        return EMDASH
    sign = "+" if v >= 0 else "-"
    cur = (currency or "").strip()
    if cur and cur != "$":
        return f"{sign}{cur} {abs(v):,.2f}"
    return f"{sign}${abs(v):,.2f}"


def format_quantity(value: Any, max_decimals: int = 8) -> str:
    """Generic quantity (contracts, units): grouped, trimmed, never invented."""
    v = _f(value)
    if v is None:
        return EMDASH
    try:
        md = max(0, min(10, int(max_decimals)))
    except (TypeError, ValueError):
        md = 8
    if v == 0:
        return "0"
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    text = f"{v:,.{md}f}".rstrip("0").rstrip(".")
    return text or "0"


def format_percent(value: Any) -> str:
    """Percentage change: +2.35% / -0.50% / —."""
    v = _f(value)
    if v is None:
        return EMDASH
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def format_lots(value: Any) -> str:
    """Lot volume: 2 decimals (0.01 lots). Distinct from money/price."""
    v = _f(value)
    if v is None:
        return EMDASH
    return f"{v:.2f}"


def format_volume(value: Any) -> str:
    """Raw volume/liquidity counts: 1.83M / 73.40B / 12.40K (no $ prefix).

    Distinct from format_money/format_market_cap on purpose: raw volume
    is a count, not account money.
    """
    v = _f(value)
    if v is None:
        return EMDASH
    if v < 0:
        return EMDASH
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if v >= threshold:
            return f"{v / threshold:,.2f}{suffix}"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def format_market_cap(value: Any) -> str:
    """Market cap / FDV sizing: $1.83M / $73.40B. None -> em dash."""
    v = _f(value)
    if v is None:
        return EMDASH
    if v < 0:
        return EMDASH
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if v >= threshold:
            return f"${v / threshold:,.2f}{suffix}"
    return f"${v:,.2f}"


def format_spread_bps(bps: Any) -> str:
    """Spread in basis points: 1.8 bps / —."""
    v = _f(bps)
    if v is None:
        return EMDASH
    if v < 0:
        return EMDASH
    return f"{v:.1f} bps"


def format_spread(value: Any, digits: Any = None) -> str:
    """Spread dispatcher: bps>=0 small floats are bps; kept for QML parity."""
    return format_spread_bps(value)


def format_spread_price(spread: Any, digits: Any = None) -> str:
    """Spread in price units (ask-bid), formatted with instrument digits."""
    v = _f(spread)
    if v is None:
        return EMDASH
    if v < 0:
        return EMDASH
    return format_price(v, digits=digits)


def format_margin_level(value: Any) -> str:
    """Margin level percent: 245.30% / —."""
    v = _f(value)
    if v is None:
        return EMDASH
    if v < 0:
        return EMDASH
    return f"{v:,.2f}%"


# -- legacy aliases (kept for existing call sites; now precision-aware) -------

def number(value: Any, digits: Any = None) -> str:
    """Legacy price alias -> format_price (magnitude-aware by default)."""
    return format_price(value, digits=digits)


def compact_money(value: Any) -> str:
    return format_market_cap(value)


def change_text(value: Any) -> str:
    return format_percent(value)


def spread_text(bps: Any) -> str:
    return format_spread_bps(bps)


def age_text(seconds: Any) -> str:
    """Honest feed-data age (not latency)."""
    v = _f(seconds)
    if v is None:
        return EMDASH
    if v < 0:
        return EMDASH
    if v < 1.0:
        return f"{v * 1000:.0f} ms"
    return f"{v:.1f}s"


def freshness_text(age_s: Any, stale: bool = False) -> str:
    """Subtle human freshness: 'Updated now' / 'Updated 4s ago' / STALE."""
    if stale:
        base = age_text(age_s)
        return f"STALE · {base}" if base != EMDASH else "STALE"
    v = _f(age_s)
    if v is None:
        return "Waiting for data"
    if v < 0:
        return "Waiting for data"
    if v < 1.5:
        return "Updated now"
    return f"Updated {age_text(v)} ago"


def feed_semantics(provider: str, source: str = "") -> str:
    """Honest streaming vs polling label. Never upgrades polling to live."""
    p = (provider or "").strip().lower()
    s = (source or "").strip()
    if p == "binance":
        return "Streaming"
    if p == "solana":
        if "polling" in s.lower():
            return s
        return "Polling"
    if p == "mt5":
        if s:
            return s
        return "MT5 polling"
    return s or EMDASH


def connection_state(connected: bool, stale: bool, state: str = "",
                     error: Any = None,
                     was_connected: bool = False) -> str:
    """Visible connection state: CONNECTING/CONNECTED/STALE/RECONNECTING/ERROR."""
    s = (state or "").upper()
    if error or s == "ERROR":
        return "RECONNECTING" if was_connected else "ERROR"
    if not connected:
        if s in ("CONNECTING", "DISCONNECTED", ""):
            return "RECONNECTING" if was_connected else "CONNECTING"
        return s or "CONNECTING"
    if stale or s == "STALE":
        return "STALE"
    if s in ("CONNECTED", "STREAMING", "LIVE"):
        return "CONNECTED"
    return s or "CONNECTED"


def bound_text(text: Any, max_len: int = 120) -> str:
    """Bound a top-bar/banner string with ellipsis (no clipping)."""
    try:
        limit = max(16, int(max_len))
    except (TypeError, ValueError):
        limit = 120
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def health_summary(providers: Any, connected: bool = False,
                   stale: bool = False, max_len: int = 120) -> str:
    """Bounded provider health summary for the top bar.

    Example: "3 feeds · Connected" / "2 feeds · Stale". Never lists every
    instrument's update age (that belongs in rows/selected asset).
    """
    try:
        names = [str(p) for p in (providers or []) if str(p)]
    except TypeError:
        names = []
    # Deduplicate preserving order, bound count display.
    seen: List[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    n = len(seen)
    if n == 0:
        base = "No feeds"
    elif n == 1:
        base = f"{seen[0]}"
    elif n <= 3:
        base = " · ".join(seen)
    else:
        base = f"{n} feeds"
    state = "Connected" if connected and not stale else (
        "Stale" if stale else "Connecting")
    if n > 3:
        out = f"{base} · {state}"
    elif n == 0:
        out = state if connected or stale else "Opening live feeds…"
    else:
        out = f"{base} · {state}"
    return bound_text(out, max_len)


# -- MT5 order/position helpers (pure; presentation only) ---------------------

def parse_volume_text(text: Any) -> Optional[float]:
    """Strict volume parse: invalid text -> None (never silently zero)."""
    if text is None:
        return None
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        v = _f(text)
        return v
    s = str(text).strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def validate_volume_text(text: Any, vol_min: Any = None,
                         vol_max: Any = None, vol_step: Any = None) -> str:
    """Human-readable volume error, or "" when valid.

    Respects broker volume_min/volume_max/volume_step. Never parses
    invalid text to zero.
    """
    v = parse_volume_text(text)
    if v is None:
        return "Enter a valid volume (numbers only)"
    if v <= 0:
        return "Volume must be greater than zero"
    mn = _f(vol_min)
    mx = _f(vol_max)
    st = _f(vol_step)
    if mn is not None and mn > 0 and v < mn - 1e-12:
        return f"Volume below broker minimum ({format_lots(mn)} lots)"
    if mx is not None and mx > 0 and v > mx + 1e-12:
        return f"Volume above broker maximum ({format_lots(mx)} lots)"
    if st is not None and st > 0:
        try:
            import math as _m
            steps = round(v / st)
            aligned = steps * st
            if abs(v - aligned) > min(st / 2.0, 1e-9) + 1e-9:
                return f"Volume must be a multiple of {format_lots(st)} lots"
        except (ValueError, OverflowError, ZeroDivisionError):
            pass
    return ""


def default_volume_text(persisted: Any, vol_min: Any = None,
                        vol_max: Any = None, vol_step: Any = None) -> str:
    """Initial volume: persisted value only if still valid, else volume_min."""
    mn = _f(vol_min)
    base_min = mn if (mn is not None and mn > 0) else 0.01
    err = validate_volume_text(persisted, vol_min, vol_max, vol_step)
    if err == "":
        v = parse_volume_text(persisted)
        if v is not None:
            return format_lots(v)
    return format_lots(base_min)


def parse_sltp_text(text: Any) -> Optional[float]:
    """Strict SL/TP parse: empty -> None (optional), invalid -> None + flag.

    Returns None for both empty and invalid; use sltp_text_error to
    distinguish. Never silently parses invalid text to zero.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        v = _f(text)
        if v is None or v <= 0:
            return None
        return v
    s = str(text).strip()
    if s == "":
        return None
    v = parse_volume_text(s)
    if v is None or v <= 0:
        return None
    return v


def sltp_text_error(text: Any, field: str = "value") -> str:
    """Inline error for an SL/TP field, or "" when empty/valid."""
    if text is None:
        return ""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        v = _f(text)
        if v is None or v <= 0:
            return f"Invalid {field} (must be a positive price)"
        return ""
    s = str(text).strip()
    if s == "":
        return ""
    v = parse_volume_text(s)
    if v is None or v <= 0:
        return f"Invalid {field} (must be a positive price)"
    return ""


def validate_sltp_side(side: str, sl: Any, tp: Any,
                       ref_price: Any) -> str:
    """Wrong-side SL/TP vs current market. Returns error or ""."""
    ref = _f(ref_price)
    if ref is None or ref <= 0:
        return ""
    s = (side or "").upper()
    if s not in ("BUY", "SELL"):
        return "Side must be BUY or SELL"
    sv = _f(sl)
    tv = _f(tp)
    if sv is not None and sv > 0:
        if s == "BUY" and sv >= ref:
            return "BUY stop-loss must be below the current price"
        if s == "SELL" and sv <= ref:
            return "SELL stop-loss must be above the current price"
    if tv is not None and tv > 0:
        if s == "BUY" and tv <= ref:
            return "BUY take-profit must be above the current price"
        if s == "SELL" and tv >= ref:
            return "SELL take-profit must be below the current price"
    return ""


def sltp_distance_text(ref_price: Any, level: Any,
                       digits: Any = None) -> str:
    """Approx distance ref->SL/TP in price units, or — when unknown."""
    ref = _f(ref_price)
    lv = _f(level)
    if ref is None or lv is None or ref <= 0 or lv <= 0:
        return EMDASH
    return format_price(abs(lv - ref), digits=digits)


def compute_notional(volume: Any, contract_size: Any,
                     price: Any) -> Optional[float]:
    """volume × contract_size × price. None when not computable; never invent."""
    v = _f(volume)
    c = _f(contract_size)
    p = _f(price)
    if v is None or c is None or p is None:
        return None
    if v <= 0 or c <= 0 or p <= 0:
        return None
    out = v * c * p
    if not math.isfinite(out):
        return None
    return out


def format_notional(volume: Any, contract_size: Any, price: Any) -> str:
    n = compute_notional(volume, contract_size, price)
    if n is None:
        return "unavailable"
    return format_money(n)


def aggregate_positions(positions: Any) -> List[Dict[str, Any]]:
    """Per-symbol aggregation (presentation only; never mutates broker state).

    Each item: {symbol, count, long_lots, short_lots, net_lots, avg_entry,
    current, floating_pl, exposure}. avg_entry is volume-weighted; current
    is the latest observed current price for the symbol; exposure needs
    contract_size (else None). Mixed direction is reported honestly as
    separate LONG/SHORT totals plus NET (no netting semantics invented).
    """
    try:
        rows = list(positions or [])
    except TypeError:
        return []
    by_sym: Dict[str, Dict[str, Any]] = {}
    for p in rows:
        try:
            if isinstance(p, dict):
                sym = str(p.get("symbol", "") or "")
                side = str(p.get("side", "") or "").upper()
                vol = _f(p.get("volume")) or 0.0
                entry = _f(p.get("entry"))
                cur = _f(p.get("current"))
                pl = _f(p.get("profit"))
                cs = _f(p.get("contract_size"))
            else:
                sym = str(getattr(p, "symbol", "") or "")
                side = str(getattr(p, "side", "") or "").upper()
                vol = _f(getattr(p, "volume", 0)) or 0.0
                entry = _f(getattr(p, "entry", None))
                cur = _f(getattr(p, "current", None))
                pl = _f(getattr(p, "profit", None))
                cs = _f(getattr(p, "contract_size", None))
        except Exception:
            continue
        if not sym or vol <= 0:
            continue
        agg = by_sym.setdefault(sym, {
            "symbol": sym, "count": 0, "long_lots": 0.0, "short_lots": 0.0,
            "net_lots": 0.0, "_wsum": 0.0, "_wvol": 0.0, "current": None,
            "floating_pl": 0.0, "_has_pl": False, "_cs": None,
        })
        agg["count"] += 1
        if side == "SELL":
            agg["short_lots"] += vol
            agg["net_lots"] -= vol
        else:  # BUY default (honest: unknown side shown as long leg)
            agg["long_lots"] += vol
            agg["net_lots"] += vol
        if entry is not None and entry > 0:
            agg["_wsum"] += entry * vol
            agg["_wvol"] += vol
        if cur is not None and cur > 0:
            agg["current"] = cur
        if pl is not None:
            agg["floating_pl"] += pl
            agg["_has_pl"] = True
        if cs is not None and cs > 0:
            agg["_cs"] = cs
    out: List[Dict[str, Any]] = []
    for sym in sorted(by_sym):
        a = by_sym[sym]
        avg = (a["_wsum"] / a["_wvol"]) if a["_wvol"] > 0 else None
        total_vol = a["long_lots"] + a["short_lots"]
        exposure = None
        if a["_cs"] and a["current"]:
            exposure = compute_notional(total_vol, a["_cs"], a["current"])
        out.append({
            "symbol": sym,
            "count": int(a["count"]),
            "long_lots": float(a["long_lots"]),
            "short_lots": float(a["short_lots"]),
            "net_lots": float(a["net_lots"]),
            "total_lots": float(total_vol),
            "avg_entry": avg,
            "current": a["current"],
            "floating_pl": float(a["floating_pl"]) if a["_has_pl"] else None,
            "exposure": exposure,
            "contract_size": a["_cs"],
        })
    return out


def sort_rows(rows: Any, field: str = "symbol",
              ascending: bool = True) -> List[dict]:
    """Pure local sort for radar rows (no network refetch).

    Fields: symbol | price | change | provider | freshness | volume.
    Unknown fields fall back to symbol. None values sort last.
    """
    try:
        data = list(rows or [])
    except TypeError:
        return []
    f = (field or "symbol").lower()
    reverse = not bool(ascending)

    def _num(r: dict, *keys: str) -> Optional[float]:
        for k in keys:
            v = _f(r.get(k))
            if v is not None:
                return v
        return None

    def key_fn(r: dict):
        if f == "price":
            v = _num(r, "price")
            return (1, 0.0) if v is None else (0, v)
        if f == "change":
            v = _num(r, "change", "change_pct_24h", "changeValue")
            has = r.get("changeUp") is not None or v is not None
            if v is None:
                return (1, 0.0)
            return (0, v)
        if f == "provider":
            return (0, str(r.get("provider", "") or "").lower(),
                    str(r.get("symbol", "") or "").lower())
        if f in ("freshness", "age", "feed"):
            v = _num(r, "age_s", "ageS", "messages")
            # Freshness: smaller age first when ascending; None last.
            if f == "freshness":
                av = _num(r, "age_s", "ageS")
                if av is None:
                    # Fall back to messages (more messages = fresher)?
                    # Keep it simple: unknown age sorts last.
                    return (1, 0.0)
                return (0, av)
            if v is None:
                return (1, "")
            return (0, v)
        if f in ("volume", "liquidity", "liquidity_usd", "volume_5m"):
            v = _num(r, "volume", "liquidity", "liquidity_usd",
                      "volume_5m", "volumeValue")
            return (1, 0.0) if v is None else (0, v)
        if f == "spread":
            v = _num(r, "spread_bps", "spreadBps", "spread")
            return (1, 0.0) if v is None else (0, v)
        # symbol (default): case-insensitive, favorites NOT reordered here
        # (caller keeps favorite flags per row; order is purely alphabetical).
        return (0, str(r.get("symbol", "") or "").lower(),
                str(r.get("key", "") or ""))

    try:
        return sorted(data, key=key_fn, reverse=reverse)
    except TypeError:
        return list(data)
