"""Paper promotion: survivors replayed through the SAME stack components.

Uses the identical strategy interface, risk manager, execution engine and
portfolio accounting as research (no separate fake implementation).
Replay is deterministic and idempotent: signal ids derive from bar
timestamps, and each id is claimed in the ResearchStore before executing —
a restarted replay with the same store executes zero new orders.
Real-money trading stays disabled (PaperExchange only).

Session identity: every paper session is keyed by a stable paper
fingerprint over (candidate, symbol, interval, dataset version/hash, full
bar history, preingest, paper config). Different symbols, datasets or
configs never share a session; an identical replay request stays
idempotent.
"""
import hashlib
import json
import logging
from typing import Dict, Any, List, Optional

from strategy import SignalType

logger = logging.getLogger(__name__)

#: Checkpoint cadence: persist paper replay state every N scored bars.
CHECKPOINT_EVERY_BARS = 50


def _order_to_dict(o) -> Dict[str, Any]:
    return {
        "order_id": o.order_id, "symbol": o.symbol,
        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
        "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
        "quantity": o.quantity, "price": o.price,
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "timestamp": o.timestamp.isoformat() if hasattr(o.timestamp, "isoformat") else str(o.timestamp),
        "filled_quantity": o.filled_quantity, "avg_fill_price": o.avg_fill_price,
        "commission": o.commission, "client_order_id": o.client_order_id,
        "reject_reason": o.reject_reason,
        "spread_cost": getattr(o, "spread_cost", 0.0),
        "slippage_cost": getattr(o, "slippage_cost", 0.0),
        "impact_cost": getattr(o, "impact_cost", 0.0),
        "liquidity_capped": bool(getattr(o, "liquidity_capped", False)),
        "participation_rate": getattr(o, "participation_rate", 0.0),
    }


def _order_from_dict(d: Dict[str, Any]):
    from exchange import Order, OrderSide, OrderType, OrderStatus
    from datetime import datetime
    ts = d.get("timestamp")
    try:
        ts = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    except (ValueError, TypeError):
        from datetime import timezone
        ts = datetime.now(timezone.utc)
    return Order(
        order_id=d["order_id"], symbol=d["symbol"],
        side=OrderSide(d["side"]), order_type=OrderType(d["order_type"]),
        quantity=d["quantity"], price=d.get("price"),
        status=OrderStatus(d["status"]), timestamp=ts,
        filled_quantity=d.get("filled_quantity", 0.0),
        avg_fill_price=d.get("avg_fill_price"),
        commission=d.get("commission", 0.0),
        client_order_id=d.get("client_order_id"),
        reject_reason=d.get("reject_reason"),
        spread_cost=d.get("spread_cost", 0.0),
        slippage_cost=d.get("slippage_cost", 0.0),
        impact_cost=d.get("impact_cost", 0.0),
        liquidity_capped=bool(d.get("liquidity_capped", False)),
        participation_rate=d.get("participation_rate", 0.0),
    )


def _risk_to_dict(m) -> Dict[str, Any]:
    out = {}
    for field in ("daily_loss", "consecutive_losses", "consecutive_wins",
                  "total_trades", "winning_trades", "losing_trades",
                  "is_in_cooldown", "peak_equity", "kill_switch_tripped",
                  "current_drawdown", "current_day"):
        out[field] = getattr(m, field, 0 if field not in
                             ("is_in_cooldown", "kill_switch_tripped") else False)
    for field in ("last_trade_time", "cooldown_end_time"):
        v = getattr(m, field, None)
        out[field] = v.isoformat() if hasattr(v, "isoformat") else None
    return out


def _risk_from_dict(risk, d: Dict[str, Any]) -> None:
    from datetime import datetime
    m = risk.metrics
    for field in ("daily_loss", "consecutive_losses", "consecutive_wins",
                  "total_trades", "winning_trades", "losing_trades",
                  "peak_equity", "current_drawdown"):
        if field in d:
            try:
                setattr(m, field, float(d[field]))
            except (TypeError, ValueError):
                pass
    for field in ("is_in_cooldown", "kill_switch_tripped"):
        if field in d:
            m.__dict__[field] = bool(d[field])
    if "current_day" in d:
        m.__dict__["current_day"] = d["current_day"]
    for field in ("last_trade_time", "cooldown_end_time"):
        v = d.get(field)
        if v:
            try:
                m.__dict__[field] = datetime.fromisoformat(v)
            except (ValueError, TypeError):
                pass
    trade_history = d.get("trade_history") or []
    risk.trade_history = list(trade_history)


def _positions_to_dict(pf) -> Dict[str, Any]:
    out = {}
    for symbol, p in pf.get_positions().items():
        out[symbol] = {
            "quantity": p.quantity, "entry_price": p.entry_price,
            "last_update": p.last_update.isoformat()
            if hasattr(p.last_update, "isoformat") else str(p.last_update),
            "unrealized_pnl": p.unrealized_pnl, "side": p.side,
        }
    return out


def _positions_from_dict(pf, d: Dict[str, Any]) -> None:
    from portfolio import Position
    from datetime import datetime, timezone
    pf.positions.clear()
    for symbol, p in (d or {}).items():
        try:
            lu = p.get("last_update")
            lu = datetime.fromisoformat(lu) if isinstance(lu, str) else lu
        except (ValueError, TypeError):
            lu = datetime.now(timezone.utc)
        pf.positions[symbol] = Position(
            symbol=symbol, quantity=float(p["quantity"]),
            entry_price=float(p["entry_price"]), last_update=lu,
            unrealized_pnl=float(p.get("unrealized_pnl", 0.0)),
            side=p.get("side", "long" if float(p["quantity"]) >= 0 else "short"))


def _rng_to_json(rng) -> Optional[list]:
    """Serialize a random.Random state (JSON-safe) for exact replay."""
    if rng is None:
        return None
    version, internal, gauss = rng.getstate()
    return [int(version), [int(x) for x in internal], gauss]


def _rng_from_json(rng, state) -> None:
    """Restore RNG state; raises loudly instead of silently diverging."""
    if state is None or rng is None:
        if state is not None:
            raise RuntimeError("checkpoint has RNG state but exchange has no RNG")
        return
    version, internal, gauss = state
    rng.setstate((int(version), tuple(int(x) for x in internal), gauss))


def paper_fingerprint(candidate, symbol: str, interval: str,
                      dataset_version: str = "",
                      dataset_hash: str = "",
                      candles=None,
                      preingest: int = 0,
                      paper_config: Optional[Dict[str, Any]] = None) -> str:
    """Stable identity for one paper replay.

    Covers candidate id + strategy (family/params), symbol, bar interval,
    dataset version/hash, the FULL ordered bar-timestamp history, the
    preingest offset and the paper config. Any change is a different
    session by construction; identical requests map to the same id
    (idempotent replays).
    """
    stamps = []
    if candles:
        for c in sorted(candles, key=lambda x: x.timestamp):
            ts = c.timestamp
            stamps.append(ts.isoformat() if hasattr(ts, "isoformat") else str(ts))
    payload = json.dumps({
        "candidate": getattr(candidate, "candidate_id", str(candidate)),
        "family": getattr(candidate, "family", ""),
        "params": getattr(candidate, "params", {}),
        "symbol": symbol,
        "interval": interval,
        "dataset_version": dataset_version or "",
        "dataset_hash": dataset_hash or "",
        "n_bars": len(stamps),
        "stamps": stamps,
        "preingest": int(preingest),
        "paper_config": paper_config or {},
    }, sort_keys=True, default=str)
    return "paper_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def _legacy_paper_id(candidate, candles, preingest: int) -> str:
    """Pre-RC3 session key (candidate + one timestamp). Kept only so old
    rows are never mistaken for new-identity sessions."""
    return (f"paper_{candidate.candidate_id}_{candles[preingest].timestamp.isoformat()}"
            if len(candles) > preingest else f"paper_{candidate.candidate_id}_empty")


def _sessions_match(ckpt: Dict[str, Any], candidate, symbol: str, candles,
                    interval: str = "5m",
                    dataset_version: str = "",
                    dataset_hash: str = "",
                    preingest: int = 0,
                    paper_config: Optional[Dict[str, Any]] = None) -> bool:
    """Guard: resume/reuse only the identical replay.

    Checks candidate + strategy, symbol, interval, dataset version/hash,
    full ordered bar history (not just boundary stamps), preingest and
    paper config. Legacy rows without the new provenance fields never
    match (safe recompute instead of silent reuse).
    """
    try:
        if ckpt.get("candidate_id") != candidate.candidate_id:
            return False
        if ckpt.get("family") != candidate.family:
            return False
        if dict(ckpt.get("params", {})) != dict(candidate.params):
            return False
        if ckpt.get("symbol") != symbol:
            return False
        if ckpt.get("interval", "5m") != interval:
            return False
        if (ckpt.get("dataset_version", "") or "") != (dataset_version or ""):
            return False
        if (ckpt.get("dataset_hash", "") or "") != (dataset_hash or ""):
            return False
        if int(ckpt.get("preingest", preingest)) != int(preingest):
            return False
        if dict(ckpt.get("paper_config", {})) != dict(paper_config or {}):
            return False
        ordered = sorted(candles, key=lambda c: c.timestamp)
        if int(ckpt.get("n_bars", -1)) != len(ordered):
            return False
        stamps = [c.timestamp.isoformat() for c in ordered]
        if list(ckpt.get("candle_stamps", [])) != stamps:
            return False
    except (IndexError, AttributeError, TypeError, ValueError):
        return False
    return True


def _candles_match(ckpt: Dict[str, Any], candidate, symbol: str, candles) -> bool:
    """Legacy guard kept for backward compatibility.

    Compares candidate, symbol, bar count and boundary stamps only. New
    code paths use :func:`_sessions_match` (full-history + provenance).
    """
    try:
        if ckpt.get("candidate_id") != candidate.candidate_id:
            return False
        if ckpt.get("symbol") != symbol:
            return False
        if int(ckpt.get("n_bars", -1)) != len(candles):
            return False
        bounds = ckpt.get("boundary_stamps", [])
        if bounds != [candles[0].timestamp.isoformat(),
                      candles[-1].timestamp.isoformat()]:
            return False
    except (IndexError, AttributeError, TypeError, ValueError):
        return False
    return True


def _snapshot_state(paper_id, candidate, symbol, candles, next_bar_index,
                    pf, ex, risk, fills, skipped_claimed,
                    equity_per_bar, fees_per_bar, fills_per_bar,
                    bar_stamps, state, interval="5m",
                    dataset_version="", dataset_hash="",
                    preingest=0, paper_config=None):
    ordered = sorted(candles, key=lambda c: c.timestamp)
    return {
        "paper_id": paper_id,
        "state": state,
        "candidate_id": candidate.candidate_id,
        "family": candidate.family,
        "params": dict(candidate.params),
        "symbol": symbol,
        "interval": interval,
        "dataset_version": dataset_version or "",
        "dataset_hash": dataset_hash or "",
        "preingest": int(preingest),
        "paper_config": dict(paper_config or {}),
        "n_bars": len(ordered),
        "candle_stamps": [c.timestamp.isoformat() for c in ordered],
        "boundary_stamps": [ordered[0].timestamp.isoformat(),
                            ordered[-1].timestamp.isoformat()] if ordered else [],
        "next_bar_index": int(next_bar_index),
        "cash": float(pf.get_cash()),
        "realized_pnl": float(pf.realized_pnl),
        "total_fees_paid": float(pf.total_fees_paid),
        "positions": _positions_to_dict(pf),
        "applied_order_ids": sorted(pf._applied_order_ids),
        "trade_history": list(getattr(pf, "trades_history", [])),
        "fills": list(fills),
        "skipped_claimed": int(skipped_claimed),
        "risk_metrics": _risk_to_dict(risk.metrics),
        "risk_trade_history": list(getattr(risk, "trade_history", [])),
        "exchange_orders": [_order_to_dict(o) for _, o in
                            sorted(ex.orders.items())],
        "exchange_by_client": dict(ex._by_client_id),
        "exchange_next_id": int(ex.next_order_id),
        "exchange_rng_state": _rng_to_json(getattr(ex, "_rng", None)),
        "equity_per_bar": list(equity_per_bar),
        "fees_per_bar": list(fees_per_bar),
        "fills_per_bar": list(fills_per_bar),
        "bar_stamps": list(bar_stamps),
    }


def _write_checkpoint(store, paper_id, candidate, symbol, candles,
                      pf, ex, risk, fills, skipped_claimed,
                      equity_per_bar, fees_per_bar, fills_per_bar,
                      bar_stamps, next_bar_index, interval="5m",
                      dataset_version="", dataset_hash="",
                      preingest=0, paper_config=None):
    if store is None:
        return
    store.save_paper_session(_snapshot_state(
        paper_id, candidate, symbol, candles, next_bar_index,
        pf, ex, risk, fills, skipped_claimed,
        equity_per_bar, fees_per_bar, fills_per_bar,
        bar_stamps, "in_progress", interval=interval,
        dataset_version=dataset_version, dataset_hash=dataset_hash,
        preingest=preingest, paper_config=paper_config))


def _rebuild_paper_state(pf, ex, risk, md, symbol, ckpt, candles, store):
    """Restore portfolio/exchange/risk/market-data from a checkpoint.

    Idempotency keys at/after the checkpoint are dropped first: any claim
    persisted by the crashed run without its fill is replayed exactly once.
    Market-data context is re-ingested in original order into the fresh
    handler (same sequence the uninterrupted run observed).
    """
    pf.cash = float(ckpt["cash"])
    pf.realized_pnl = float(ckpt["realized_pnl"])
    pf.total_fees_paid = float(ckpt["total_fees_paid"])
    _positions_from_dict(pf, ckpt.get("positions", {}))
    pf._applied_order_ids = set(ckpt.get("applied_order_ids", []))
    pf.trades_history = list(ckpt.get("trade_history", []))
    ex.orders.clear()
    for od in ckpt.get("exchange_orders", []):
        o = _order_from_dict(od)
        ex.orders[o.order_id] = o
    ex._by_client_id = dict(ckpt.get("exchange_by_client", {}))
    ex.next_order_id = int(ckpt.get("exchange_next_id", len(ex.orders) + 1))
    _rng_from_json(getattr(ex, "_rng", None), ckpt.get("exchange_rng_state"))
    _risk_from_dict(risk, ckpt.get("risk_metrics", {}))
    risk.trade_history = list(ckpt.get("risk_trade_history", []))
    if store is not None:
        # Session-scoped cleanup: only this (candidate, symbol) replay's
        # keys are dropped. Other symbols' sessions are never touched.
        store.delete_idempotency_like(
            f"paper_{ckpt.get('candidate_id', '')}_{ckpt.get('symbol', '')}_")
    for c in candles[:int(ckpt.get("next_bar_index", 0))]:
        md.ingest_candles(symbol, ckpt.get("interval", "5m") or "5m", [c])


async def run_paper_replay(candidate, candles, stack_factory: callable,
                           symbol: str, store=None,
                           preingest: int = 30,
                           checkpoint_every: int = CHECKPOINT_EVERY_BARS,
                           resume: bool = True,
                           fault_after_bars: Optional[int] = None,
                           interval: str = "5m",
                           dataset_version: str = "",
                           dataset_hash: str = "",
                           paper_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Replay bars through paper components. Returns the paper session.

    Session identity is the stable paper fingerprint over (candidate,
    symbol, interval, dataset version/hash, full bar history, preingest,
    paper config): different symbols or datasets never share a session,
    while an identical replay request stays idempotent.

    Crash recovery: progress checkpoints (cash, positions, fills, orders,
    risk metrics, series) are persisted every `checkpoint_every` scored bars
    plus an initial checkpoint before any claim. A restart with the same
    store rebuilds exact state and continues from the next unscored bar;
    idempotency keys for this session are dropped first, so a
    crash-after-claim-before-fill can never lose nor duplicate a fill.
    A matching completed checkpoint is returned as-is: restarts never reset
    equity or overwrite results. `fault_after_bars` is a crash-recovery test hook
    (raises RuntimeError after that many scored bars).
    """
    paper_config = dict(paper_config or {})
    candles = sorted(candles, key=lambda c: c.timestamp)
    paper_id = paper_fingerprint(
        candidate, symbol, interval,
        dataset_version=dataset_version, dataset_hash=dataset_hash,
        candles=candles, preingest=preingest, paper_config=paper_config)
    if store is not None and resume:
        done = store.load_paper_session(paper_id)
        if (isinstance(done, dict) and done.get("state") == "completed"
                and _sessions_match(done, candidate, symbol, candles,
                                    interval=interval,
                                    dataset_version=dataset_version,
                                    dataset_hash=dataset_hash,
                                    preingest=preingest,
                                    paper_config=paper_config)):
            return done
    config, md, ex, risk, pf, eng, strat, bt = stack_factory()
    from research.campaign import SingleStrategyManager
    from research import strategies as fam
    strategy = fam.build_strategy(candidate.family, candidate.params)
    manager = SingleStrategyManager(strategy)
    await ex.connect()
    await md.connect()
    try:
        fills: List[Dict[str, Any]] = []
        skipped_claimed = 0
        equity_per_bar: List[float] = []
        fees_per_bar: List[float] = []
        fills_per_bar: List[int] = []
        bar_stamps: List[str] = []
        scored = 0
        start_index = preingest
        resumed = False
        if store is not None and resume:
            ckpt = store.load_paper_session(paper_id)
            if (isinstance(ckpt, dict) and ckpt.get("state") == "in_progress"
                    and _sessions_match(ckpt, candidate, symbol, candles,
                                        interval=interval,
                                        dataset_version=dataset_version,
                                        dataset_hash=dataset_hash,
                                        preingest=preingest,
                                        paper_config=paper_config)):
                _rebuild_paper_state(pf, ex, risk, md, symbol, ckpt, candles, store)
                fills = list(ckpt.get("fills", []))
                skipped_claimed = int(ckpt.get("skipped_claimed", 0))
                equity_per_bar = list(ckpt.get("equity_per_bar", []))
                fees_per_bar = list(ckpt.get("fees_per_bar", []))
                fills_per_bar = list(ckpt.get("fills_per_bar", []))
                bar_stamps = list(ckpt.get("bar_stamps", []))
                scored = len(bar_stamps)
                start_index = int(ckpt.get("next_bar_index", preingest))
                resumed = True
                logger.info("Resuming paper %s at bar %d/%d",
                            paper_id, start_index, len(candles))
            else:
                if ckpt is not None:
                    logger.warning("Paper checkpoint mismatch for %s — fresh replay",
                                   paper_id)
                    store.delete_idempotency_like(
                        f"paper_{candidate.candidate_id}_{symbol}_")
                _write_checkpoint(store, paper_id, candidate, symbol, candles,
                                  pf, ex, risk, fills, skipped_claimed,
                                  equity_per_bar, fees_per_bar, fills_per_bar,
                                  bar_stamps, preingest, interval=interval,
                                  dataset_version=dataset_version,
                                  dataset_hash=dataset_hash,
                                  preingest=preingest,
                                  paper_config=paper_config)
        # Context so the engine never falls back to mock bars. On resume the
        # prefix was already re-ingested by _rebuild_paper_state.
        if not resumed:
            for c in candles[:start_index]:
                md.ingest_candles(symbol, interval, [c])
        for pos in range(start_index, len(candles)):
            bar = candles[pos]
            md.ingest_candles(symbol, interval, [bar])
            window = await md.get_candles(symbol, interval, 100)
            if len(window) < 6:
                continue
            sig = manager.generate_signal(symbol, {"candles": window})
            n_before = len(fills)
            if sig is not None and sig.signal_type != SignalType.HOLD:
                sig.signal_id = (f"paper_{candidate.candidate_id}_{symbol}_"
                                 f"{bar.timestamp.isoformat()}_{sig.signal_type.value}")
                sig.price = bar.close
                if store is not None and not store.claim_idempotency(sig.signal_id, ""):
                    skipped_claimed += 1
                else:
                    res = await eng.execute_signal(sig, pf, {symbol: bar.close},
                                                   sim_now=bar.timestamp)
                    if res.success:
                        if store is not None:
                            # Record the definitive order id against the claimed key.
                            store.claim_idempotency(sig.signal_id + ":done", res.order_id)
                        fills.append({"order_id": res.order_id,
                                      "filled_quantity": res.filled_quantity,
                                      "avg_fill_price": res.avg_fill_price,
                                      "commission": res.commission})
            equity_per_bar.append(float(pf.get_equity({symbol: bar.close})))
            fees_per_bar.append(float(pf.total_fees_paid))
            fills_per_bar.append(len(fills) - n_before)
            bar_stamps.append(bar.timestamp.isoformat())
            scored += 1
            if store is not None and checkpoint_every > 0 and scored % checkpoint_every == 0:
                _write_checkpoint(store, paper_id, candidate, symbol, candles,
                                  pf, ex, risk, fills, skipped_claimed,
                                  equity_per_bar, fees_per_bar, fills_per_bar,
                                  bar_stamps, next_bar_index=pos + 1,
                                  interval=interval,
                                  dataset_version=dataset_version,
                                  dataset_hash=dataset_hash,
                                  preingest=preingest,
                                  paper_config=paper_config)
            if fault_after_bars is not None and scored >= fault_after_bars:
                raise RuntimeError(f"injected crash after {scored} scored bars (test hook)")
        info = pf.calculate_pnl({symbol: candles[-1].close}) if candles else None
        session = {
            "paper_id": paper_id,
            "state": "completed",
            "candidate_id": candidate.candidate_id,
            "family": candidate.family,
            "params": dict(candidate.params),
            "symbol": symbol,
            "interval": interval,
            "dataset_version": dataset_version or "",
            "dataset_hash": dataset_hash or "",
            "preingest": int(preingest),
            "paper_config": dict(paper_config),
            "n_bars": len(candles),
            "candle_stamps": [c.timestamp.isoformat() for c in candles],
            "boundary_stamps": [candles[0].timestamp.isoformat(),
                                candles[-1].timestamp.isoformat()] if candles else [],
            "n_fills": len(fills),
            "fills": fills,
            "skipped_claimed": skipped_claimed,
            "paper_net": float(pf.realized_pnl),
            "final_equity": float(info.total_value) if info else 0.0,
            "fees_paid": float(pf.total_fees_paid),
            "equity_per_bar": equity_per_bar,
            "fees_per_bar": fees_per_bar,
            "fills_per_bar": fills_per_bar,
            "bar_stamps": bar_stamps,
        }
        if store is not None:
            store.save_paper_session(session)
        return session
    finally:
        try:
            await md.disconnect()
        except Exception:
            pass
        try:
            await ex.disconnect()
        except Exception:
            pass


async def paper_with_divergence(candidate, candles, stack_factory: callable,
                                symbol: str, store=None,
                                interval: str = "5m",
                                dataset_version: str = "",
                                dataset_hash: str = "",
                                paper_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paper replay + backtest on identical bars; reports divergence."""
    session = await run_paper_replay(candidate, candles, stack_factory, symbol, store,
                                     interval=interval,
                                     dataset_version=dataset_version,
                                     dataset_hash=dataset_hash,
                                     paper_config=paper_config)
    # Backtest baseline on the same bars (fresh stack, no warmup: both see
    # identical trailing history; divergence isolates engine timing).
    from research import strategies as fam
    config, md, ex, risk, pf, eng, strat, bt = stack_factory()
    from research.campaign import SingleStrategyManager
    bt.strategy_manager = SingleStrategyManager(
        fam.build_strategy(candidate.family, candidate.params))
    await ex.connect()
    try:
        res = await bt.run(symbol, list(candles))
    finally:
        try:
            await ex.disconnect()
        except Exception:
            pass
    session["backtest_net"] = float(res.realized_pnl)
    session["divergence"] = session["paper_net"] - float(res.realized_pnl)
    session["rolling_divergence"] = rolling_divergence(
        {"net": float(res.realized_pnl), "fills": res.trades,
         "fees": float(getattr(res, 'fees_paid', 0.0)),
         "n_bars": max(len(session.get("bar_stamps", [])), 1)},
        session.get("equity_per_bar", []), session.get("fills_per_bar", []),
        session.get("fees_per_bar", []))
    if store is not None:
        store.save_paper_session(session)
    return session


def rolling_divergence(expected: Dict[str, float],
                       equity_per_bar: List[float],
                       fills_per_bar: List[int],
                       fees_per_bar: List[float],
                       window_bars: int = 20) -> Dict[str, Any]:
    """Rolling paper-vs-expectation divergence. Pure function.

    Expected totals are prorated per window. Warnings (all guarded by
    minimum-sample floors, documented):
    - trade frequency ratio outside [0.5, 2.0] with >= 3 expected fills;
    - net PnL sign flip vs expectation when |expected| >= 1.0 quote;
    - paper fees more than 2x expected (expected > 0).
    """
    n = len(equity_per_bar)
    total_bars = max(n, 1)
    exp_net = float(expected.get("net", 0.0))
    exp_fills = int(expected.get("fills", 0))
    exp_fees = float(expected.get("fees", 0.0))
    windows = []
    i = 0
    while i < n:
        seg_eq = equity_per_bar[i:i + window_bars]
        seg_fills = fills_per_bar[i:i + window_bars] if fills_per_bar else [0] * len(seg_eq)
        seg_fees = fees_per_bar[i:i + window_bars] if fees_per_bar else [0.0] * len(seg_eq)
        wlen = len(seg_eq)
        frac = wlen / total_bars
        actual_net = seg_eq[-1] - seg_eq[0] if wlen > 1 else 0.0
        expected_net = exp_net * frac
        actual_fills = sum(seg_fills)
        expected_fills = exp_fills * frac
        actual_fees = (seg_fees[-1] - seg_fees[0]) if wlen > 1 else 0.0
        expected_fees = exp_fees * frac
        warns = []
        if expected_fills >= 3 and not (0.5 * expected_fills <= actual_fills
                                        <= 2.0 * expected_fills):
            warns.append(f"freq_divergence:actual={actual_fills}:expected={expected_fills:.1f}")
        if abs(expected_net) >= 1.0 and actual_net * expected_net < 0:
            warns.append(f"net_sign_flip:actual={actual_net:.2f}:expected={expected_net:.2f}")
        if expected_fees > 0 and actual_fees > 2.0 * expected_fees:
            warns.append(f"cost_divergence:actual={actual_fees:.2f}:expected={expected_fees:.2f}")
        windows.append({"start_bar": i, "bars": wlen, "actual_net": actual_net,
                        "expected_net": expected_net, "actual_fills": actual_fills,
                        "expected_fills": expected_fills, "warnings": warns})
        i += window_bars
    all_warns = [w for win in windows for w in win["warnings"]]
    return {"windows": windows, "n_warnings": len(all_warns), "warnings": all_warns}
