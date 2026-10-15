"""Market-regime analysis without future leakage.

Labels are computed from trailing windows only: the label of bar i uses
bars (i-window, i] — never anything after i. Closed-trade PnL is attributed
to the regime of its exit bar. Simple, interpretable, no ML (yet).
"""
import math
from typing import Dict, List, Any

TREND_UP = "trending_up"
TREND_DOWN = "trending_down"
SIDEWAYS = "sideways"
HIGH_VOL = "high_volatility"
LOW_VOL = "low_volatility"

REGIMES = (TREND_UP, TREND_DOWN, SIDEWAYS, HIGH_VOL, LOW_VOL)


def classify_bar(history_closes: List[float], lookback: int = 20,
                 slope_thr: float = 0.02, vol_thr: float = 0.02) -> Dict[str, str]:
    """Label one bar from trailing closes. Pure function (no leakage possible)."""
    window = history_closes[-lookback:] if len(history_closes) >= lookback else list(history_closes)
    if len(window) < 3 or window[0] <= 0:
        return {"trend": SIDEWAYS, "vol": LOW_VOL}
    drift = (window[-1] - window[0]) / window[0]
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
    rel_vol = math.sqrt(max(var, 0.0)) / mean if mean > 0 else 0.0
    trend = TREND_UP if drift > slope_thr else (TREND_DOWN if drift < -slope_thr else SIDEWAYS)
    vol = HIGH_VOL if rel_vol > vol_thr else LOW_VOL
    return {"trend": trend, "vol": vol}


def label_series(candles, lookback: int = 20, **kwargs) -> List[Dict[str, Any]]:
    """Label every bar; label[i] uses closes[:i+1] only."""
    closes = [c.close for c in candles]
    return [{"timestamp": c.timestamp.isoformat(),
             **classify_bar(closes[:i + 1], lookback, **kwargs)}
            for i, c in enumerate(candles)]


def attribute_trades(result, candles, lookback: int = 20, **kwargs) -> Dict[str, Dict[str, float]]:
    """Attribute each closed trade to its exit-bar regime.

    Returns {regime: {n, pnl, wins, losses, gross_win, gross_loss}}.
    pnls with missing stamps are unattributed (counted under 'unknown')
    — never silently dropped.
    """
    labels = {lbl["timestamp"]: lbl for lbl in label_series(candles, lookback, **kwargs)}
    pnls = list(getattr(result, 'trade_pnls', []) or [])
    stamps = list(getattr(result, 'trade_exit_stamps', []) or [])
    table: Dict[str, Dict[str, float]] = {
        r: {"n": 0, "pnl": 0.0, "wins": 0, "losses": 0,
            "gross_win": 0.0, "gross_loss": 0.0} for r in REGIMES + ("unknown",)}
    for idx, pnl in enumerate(pnls):
        stamp = stamps[idx] if idx < len(stamps) else ""
        lbl = labels.get(stamp)
        if lbl is None:
            _add(table["unknown"], pnl)
            continue
        for regime in (lbl["trend"], lbl["vol"]):
            _add(table[regime], pnl)
    return table


def _add(cell: Dict[str, float], pnl: float) -> None:
    cell["n"] += 1
    cell["pnl"] += pnl
    if pnl > 1e-9:
        cell["wins"] += 1
        cell["gross_win"] += pnl
    elif pnl < -1e-9:
        cell["losses"] += 1
        cell["gross_loss"] += pnl


def regime_report_table(attribution: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """Per-regime {n, net, expectancy, profit_factor} for reports."""
    table = {}
    for regime, agg in attribution.items():
        n = int(agg.get("n", 0))
        gw = float(agg.get("gross_win", 0.0))
        gl = float(agg.get("gross_loss", 0.0))
        table[regime] = {
            "n": n,
            "net": float(agg.get("pnl", 0.0)),
            "expectancy": (float(agg.get("pnl", 0.0)) / n) if n else 0.0,
            "profit_factor": (gw / abs(gl)) if gl < -1e-9 else (float("inf") if gw > 0 else 0.0),
        }
    return table


def regime_drawdowns(result, candles, lookback: int = 20, **kwargs) -> Dict[str, float]:
    """Max drawdown within each regime's contiguous equity runs.

    Equity points are mapped to regimes via their exec-bar timestamps
    (trailing-only labels). A regime's DD is the worst peak-to-trough
    inside any single contiguous run of that regime.
    """
    labels = {lbl["timestamp"]: (lbl["trend"], lbl["vol"])
              for lbl in label_series(candles, lookback, **kwargs)}
    curve = list(getattr(result, 'equity_curve', []) or [])
    stamps = list(getattr(result, 'equity_stamps', []) or [])
    worst: Dict[str, float] = {r: 0.0 for r in REGIMES}
    # Group consecutive points by (trend) regime; vol tracked jointly.
    runs: List[List[float]] = []
    run_labels: List[tuple] = []
    for eq, stamp in zip(curve, stamps):
        lbl = labels.get(stamp)
        if lbl is None:
            continue
        if run_labels and run_labels[-1] == lbl:
            runs[-1].append(eq)
        else:
            runs.append([eq])
            run_labels.append(lbl)
    for series, lbl in zip(runs, run_labels):
        peak = series[0]
        dd = 0.0
        for eq in series[1:]:
            peak = max(peak, eq)
            if peak > 0:
                dd = max(dd, (peak - eq) / peak)
        for regime in lbl:
            worst[regime] = max(worst[regime], dd)
    return worst


def regime_concentration(attribution: Dict[str, Dict[str, float]]) -> float:
    """Share of |gross| from the single best trend regime (0..1)."""
    trend = {k: abs(v["pnl"]) for k, v in attribution.items()
             if k in (TREND_UP, TREND_DOWN, SIDEWAYS)}
    tot = sum(trend.values())
    return (max(trend.values()) / tot) if tot > 0 else 0.0


def regime_distribution(candles, lookback: int = 20, **kwargs) -> Dict[str, Any]:
    """Diagnostics: label mix overall and per calendar year (trailing-only).

    A classifier covering ~everything with one regime is a red flag this
    surfaces explicitly (see `dominant_share`).
    """
    labels = label_series(candles, lookback, **kwargs)
    total = len(labels)
    by_trend: Dict[str, int] = {}
    by_vol: Dict[str, int] = {}
    by_year: Dict[str, Dict[str, int]] = {}
    for c, lbl in zip(candles, labels):
        by_trend[lbl["trend"]] = by_trend.get(lbl["trend"], 0) + 1
        by_vol[lbl["vol"]] = by_vol.get(lbl["vol"], 0) + 1
        yr = str(c.timestamp.year)
        bucket = by_year.setdefault(yr, {})
        bucket[lbl["trend"]] = bucket.get(lbl["trend"], 0) + 1
    trend_share = {k: v / total for k, v in by_trend.items()} if total else {}
    return {
        "n_bars": total,
        "trend_share": trend_share,
        "vol_share": {k: v / total for k, v in by_vol.items()} if total else {},
        "by_year": {y: {k: v / sum(b.values()) for k, v in b.items()}
                    for y, b in sorted(by_year.items())},
        "dominant_share": max(trend_share.values()) if trend_share else 0.0,
    }
