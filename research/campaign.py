"""Autonomous research campaigns: generate -> gate -> promote (or reject).

Every number in the funnel comes from actual execution. Every rejection is
persisted with a machine-readable reason. Strategy params are never tuned
here beyond the declared search spaces.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable

from research.candidates import (
    StrategyCandidate, CandidateStatus, candidate_fingerprint,
)
from research import strategies as fam
from research.generate import random_search, make_candidate
from research.datasets import ResearchDataset, FINAL_MODE
from research import gates
from research.gates import GateConfig
from research.scoring import research_score
from research.regimes import attribute_trades, regime_concentration, regime_report_table, regime_report_table
from research.stability import neighbourhood_report
from research.store import ResearchStore
from evaluation import (
    temporal_split, walk_forward_folds, compute_report_metrics,
    run_cost_sweep, estimate_capacity,
)

logger = logging.getLogger(__name__)

# Single source of truth for the research cost model. The campaign cost
# sweep multiplies these base values; the same dict feeds the experiment
# fingerprint so cost assumptions are part of experiment identity.
BASE_COST_MODEL = {'taker_fee': 0.002, 'maker_fee': 0.001,
                   'spread_pct': 0.0005, 'slippage_pct': 0.0005}

STAGES = [
    CandidateStatus.TRAIN_EVALUATED.value,
    CandidateStatus.VALIDATED.value,
    CandidateStatus.WALK_FORWARD_PASSED.value,
    CandidateStatus.COST_STRESS_PASSED.value,
    CandidateStatus.REGIME_PASSED.value,
    CandidateStatus.CAPACITY_PASSED.value,
    CandidateStatus.PAPER_CANDIDATE.value,
]


class SingleStrategyManager:
    """Minimal strategy_manager shim around one candidate strategy."""

    def __init__(self, strategy):
        self._strategy = strategy

    def generate_signal(self, symbol, market_data, strategy_name=None):
        return self._strategy.calculate_signal(symbol, market_data)


@dataclass
class CampaignConfig:
    campaign_id: str
    symbol: str
    families: List[str]
    candidate_limit: int = 16
    seed: int = 7
    symbols: List[str] = field(default_factory=list)  # default: [symbol]
    train_ratio: float = 0.6
    validation_ratio: float = 0.2
    embargo_bars: int = 5
    warmup_bars: int = 30
    n_splits: int = 2
    train_bars: int = 60
    test_bars: int = 40
    execution_model: str = 'next_close'
    cost_multipliers: List[float] = field(default_factory=lambda: [1, 1.5, 2, 3, 5])
    capacity_multipliers: List[float] = field(default_factory=lambda: [1, 5, 20])
    stability_neighbours: int = 4
    min_capacity_capital: float = 20000.0
    allow_holdout: bool = False
    gates: GateConfig = field(default_factory=GateConfig)
    # Optional BO search (OFF by default; random/grid remain fully supported).
    # When 'bayes': per-family GP+EI rounds on the TRAIN slice only, then the
    # top_k by train objective enter the standard funnel. Never touches
    # validation, walk-forward, or holdout data for search decisions.
    optimizer: str = 'random'
    optimizer_rounds: int = 2
    optimizer_batch: int = 4
    optimizer_top_k: int = 8


def campaign_symbols(cfg: CampaignConfig) -> List[str]:
    """Effective symbol universe (legacy single-symbol configs keep working)."""
    syms = list(cfg.symbols) if cfg.symbols else [cfg.symbol]
    if cfg.symbol not in syms:
        syms = [cfg.symbol] + syms
    return syms


async def _run_backtest(symbol, candles, strategy, stack_factory,
                        execution_model, warmup=None, costs=None):
    """Fresh stack + candidate strategy. Returns (result, metrics, initial)."""
    config, md, ex, risk, pf, eng, strat, bt = stack_factory(costs or {})
    bt.strategy_manager = SingleStrategyManager(strategy)
    if hasattr(eng, 'risk_manager') and eng.risk_manager is None:
        eng.set_risk_manager(risk)
    await ex.connect()
    try:
        res = await bt.run(symbol, list(candles), execution_model=execution_model,
                           warmup_candles=list(warmup) if warmup else None)
    finally:
        try:
            await ex.disconnect()
        except Exception:
            pass
    initial = float(config.get('initial_cash', 10000.0))
    return res, compute_report_metrics(res, initial), initial


def _stack_factory_with(cost_base_factory, candidate_strategy=None, extra=None):
    """Adapt a costs-accepting factory to the campaign's needs.

    Contract (same as `evaluation.run_cost_sweep`): `cost_base_factory`
    accepts an optional costs/overrides dict which it MUST apply BEFORE
    constructing exchange/risk/portfolio components (those snapshot config
    at construction; post-construction `cfg.set` is silently ignored).
    Merged overrides (extra, then per-call costs) are forwarded; the
    candidate strategy is installed on the engine post-construction, which
    is safe because `BacktestEngine.run` reads `strategy_manager` live.
    """
    def build(costs=None):
        merged = dict(extra or {})
        merged.update(costs or {})
        try:
            out = cost_base_factory(merged)
        except TypeError as e:
            raise TypeError(
                "stack_factory must accept an optional costs/overrides dict "
                f"applied before component construction: {e}")
        if candidate_strategy is not None:
            out[-1].strategy_manager = SingleStrategyManager(candidate_strategy)
        return out
    return build


async def _bayes_search(cfg, primary, train_bars, stack_factory, store,
                        dataset_version=""):
    """Optional GP+EI search on the primary TRAIN slice only.

    Runs `optimizer_rounds` x `optimizer_batch` trials per family, scores
    each with the multi-metric train objective, persists every trial, and
    returns the top_k candidates for the standard funnel. Never touches
    validation, walk-forward, or holdout data.
    """
    from research.optimizer import BayesianOptimizer, train_objective
    trials = []
    scored = []
    for fi, family in enumerate(sorted(cfg.families)):
        opt = BayesianOptimizer(family, seed=cfg.seed * 1009 + fi)
        history = []
        for _ in range(max(1, cfg.optimizer_rounds)):
            for params in opt.propose(history, max(1, cfg.optimizer_batch)):
                cand = make_candidate(family, params, cfg.seed * 100003 + len(trials),
                                      dataset_version=dataset_version)
                strategy = fam.build_strategy(family, params)
                res, m, _ = await _run_backtest(
                    primary, train_bars, strategy,
                    _stack_factory_with(stack_factory),
                    cfg.execution_model, warmup=None)
                score = train_objective(m)
                history.append({"params": params, "score": score})
                trial = {"family": family, "params": params,
                         "train_objective": score,
                         "train_metrics": {k: m.get(k) for k in
                                           ("n_closed", "expectancy", "profit_factor",
                                            "max_drawdown", "net_pnl")}}
                trials.append(trial)
                # Trials are namespaced by trial id (never as candidates), so
                # the funnel's fingerprint dedup cannot mistake them for
                # evaluated candidates. Tagged with the campaign for audit.
                store.save_evaluation(f"trial_{cand.candidate_id}", "optimizer_trial",
                                      score > 0, "", trial,
                                      campaign_id=cfg.campaign_id)
                scored.append((score, family, params))
    scored.sort(key=lambda t: t[0], reverse=True)
    cands = [make_candidate(family, params, cfg.seed * 100003 + 100000 + i,
                            dataset_version=dataset_version)
             for i, (_, family, params) in enumerate(scored[:max(1, cfg.optimizer_top_k)])]
    return cands, trials


async def run_campaign(cfg: CampaignConfig, dataset: ResearchDataset,
                       stack_factory: Callable, store: Optional[ResearchStore] = None,
                       on_candidate=None,
                       extra_datasets: Optional[Dict[str, ResearchDataset]] = None,
                       on_holdout_access=None) -> Dict[str, Any]:
    """Execute the full research funnel. Returns the campaign record."""
    from research.candidates import _utcnow
    store = store or ResearchStore(":memory:")
    started_at = _utcnow().isoformat()
    symbols = campaign_symbols(cfg)
    ds_by_symbol: Dict[str, ResearchDataset] = {dataset.symbol: dataset}
    for s, ds in (extra_datasets or {}).items():
        if s in ds_by_symbol:
            raise ValueError(f"duplicate dataset for symbol {s}")
        ds_by_symbol[s] = ds
    if True:
        for sym, ds in ds_by_symbol.items():
            ds.attach_access_logger(
                _make_holdout_access_cb(store, cfg.campaign_id, sym,
                                        on_holdout_access))
    missing = [s for s in symbols if s not in ds_by_symbol]
    if missing:
        raise ValueError(f"Missing datasets for symbols: {missing}")
    primary = symbols[0]
    dataset_version = ds_by_symbol[primary].version
    versions = {s: ds_by_symbol[s].version for s in symbols}
    # Per-symbol splits (train/valid + WF folds share the protocol).
    splits: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        ds = ds_by_symbol[sym]
        research = sorted(ds.research_bars, key=lambda c: c.timestamp)
        train, valid, test_unused = temporal_split(
            research, cfg.train_ratio, cfg.validation_ratio, embargo_bars=cfg.embargo_bars)
        wf_folds = walk_forward_folds(
            research, cfg.n_splits, cfg.train_bars, cfg.test_bars,
            embargo_bars=cfg.embargo_bars, warmup_bars=cfg.warmup_bars)
        for fold in wf_folds:
            ds.assert_not_holdout(fold['test'], label=f"walk-forward test [{sym}]")
        ds.assert_not_holdout(train, f"train [{sym}]")
        ds.assert_not_holdout(valid, f"validation [{sym}]")
        splits[sym] = {"dataset": ds, "train": train, "valid": valid, "wf_folds": wf_folds}
    if (cfg.optimizer or 'random') == 'bayes':
        cands, opt_trials = await _bayes_search(cfg, primary, splits[primary]["train"],
                                                stack_factory, store,
                                                dataset_version=dataset_version)
    else:
        if cfg.optimizer not in ('random', None, ''):
            raise ValueError(f"Unknown optimizer {cfg.optimizer!r} (use 'random' or 'bayes')")
        cands = random_search(cfg.families, cfg.candidate_limit, cfg.seed,
                              dataset_version=dataset_version)
        opt_trials = []

    funnel = {s: {"passed": 0, "rejected": 0} for s in STAGES}
    funnel["cross_market"] = {"passed": 0, "rejected": 0}
    funnel["generated"] = len(cands)
    funnel["duplicates_skipped"] = 0
    funnel["previously_evaluated"] = 0
    promoted = []
    eff_cfg = effective_config(cfg, ds_by_symbol, symbols)
    # Publish the campaign record immediately (state in_progress) so a crash
    # always leaves a recoverable trace, not a silent gap.
    store.save_campaign({
        "campaign_id": cfg.campaign_id, "state": "in_progress",
        "symbol": primary, "symbols": symbols,
        "dataset_version": dataset_version, "dataset_versions": versions,
        "families": list(cfg.families), "candidate_limit": cfg.candidate_limit,
        "seed": cfg.seed, "funnel": funnel, "promoted": [],
        "gates": dict(eff_cfg["gates"]), "allow_holdout": cfg.allow_holdout,
        "holdout_accesses": [], "optimizer": cfg.optimizer or 'random',
        "optimizer_trials": len(opt_trials),
        "effective_config": eff_cfg,
        "protocol_version": eff_cfg["protocol_version"],
    })
    # Experiment-identity duplicate defense, resume-aware. Strategy
    # identity (candidate fingerprint) and experiment identity (strategy x
    # dataset x costs x gates x splits x seed x protocol) are different
    # things: the same strategy may legitimately appear in many experiments.
    # Completed work is deduplicated by EXPERIMENT fingerprint only:
    #  - same campaign + completed experiment row -> previously_evaluated;
    #  - same campaign + incomplete experiment row -> cleaned and redone
    #    deterministically below (crash resume);
    #  - other campaign + completed experiment with the SAME experiment
    #    fingerprint -> previously_evaluated (genuine reuse);
    #  - same strategy but a DIFFERENT experiment fingerprint (new dataset,
    #    costs, gates, splits, seed, ...) -> a new experiment, evaluated.
    # In-list repeats share the legacy duplicates_skipped counter.
    seen_exp_fp = set()

    for cand in cands:
        cand.experiment_fingerprint = _experiment_fp(
            eff_cfg, cand.fingerprint, cfg.seed)
        exp = store.get_experiment(cfg.campaign_id, cand.candidate_id)
        if (exp is not None and exp["state"] == "completed"
                and exp.get("fingerprint") == cand.experiment_fingerprint):
            funnel["previously_evaluated"] += 1
            seen_exp_fp.add(cand.experiment_fingerprint)
            continue
        if exp is not None:
            # Crash/failed resume (incomplete row) or stale row from an
            # earlier run of this campaign_id under a different experiment
            # configuration (fingerprint mismatch): drop the partial/stale
            # attempt; the deterministic re-run below reproduces the exact
            # same evaluations.
            store.delete_evaluations(cand.candidate_id, cfg.campaign_id)
            seen_exp_fp.discard(cand.experiment_fingerprint)
        elif _is_complete(store, cfg.campaign_id, cand):
            funnel["previously_evaluated"] += 1
            seen_exp_fp.add(cand.experiment_fingerprint)
            continue
        if store.find_completed_experiment_by_fingerprint(
                cand.experiment_fingerprint,
                exclude_campaign_id=cfg.campaign_id) is not None:
            funnel["previously_evaluated"] += 1
            seen_exp_fp.add(cand.experiment_fingerprint)
            continue
        if cand.experiment_fingerprint in seen_exp_fp:
            funnel["duplicates_skipped"] += 1
            continue
        seen_exp_fp.add(cand.experiment_fingerprint)
        cand.experiment_fingerprint = _experiment_fp(
            eff_cfg, cand.fingerprint, cfg.seed)
        store.upsert_experiment(cfg.campaign_id, cand.candidate_id,
                                cand.experiment_fingerprint, "", "in_progress")
        try:
            strategy = fam.build_strategy(cand.family, cand.params)
        except ValueError as e:
            cand.record_evaluation("generation", {}, False, f"invalid_params:{e}")
            store.save_candidate(cand)
            store.upsert_experiment(cfg.campaign_id, cand.candidate_id,
                                    cand.experiment_fingerprint,
                                    "generation", "completed")
            continue
        store.save_candidate(cand)
        if on_candidate is not None:
            on_candidate(cand, "generated")

        P = splits[primary]
        if not await _gate_train(cfg, P["dataset"], cand, strategy, stack_factory,
                                 P["train"], store, funnel, on_candidate,
                                 symbol=primary):
            continue
        train_m = cand.evaluations[-1]["metrics"]
        gate_out = await _gate_valid(cfg, P["dataset"], cand, strategy, stack_factory,
                                     P["train"], P["valid"], train_m, store, funnel,
                                     on_candidate, symbol=primary)
        if not gate_out[0]:
            continue
        valid_res = gate_out[1]
        valid_m = cand.evaluations[-1]["metrics"]
        # Secondary-market confirmation BEFORE expensive primary stages
        # (fail fast on single-market wonders).
        secondary = await _confirm_secondary(cfg, cand, strategy, stack_factory,
                                             splits, primary, store)
        if secondary is None:
            _bump(funnel, "cross_market", False)
            continue
        _bump(funnel, "cross_market", True)
        secondary_details = secondary["details"]
        wf_res = await _gate_walk_forward(cfg, P["dataset"], cand, stack_factory,
                                          P["wf_folds"], store, funnel, on_candidate,
                                          symbol=primary)
        if wf_res is None:
            continue
        if not await _gate_costs(cfg, P["dataset"], cand, strategy, stack_factory,
                                 P["valid"], store, funnel, on_candidate,
                                 symbol=primary):
            continue
        stress = cand.evaluations[-1]["metrics"]
        concentration = await _gate_regime(cfg, cand, store, funnel,
                                           P["valid"], valid_res, valid_m,
                                           secondary=secondary_details)
        if concentration is None:
            continue
        cap_summary = await _gate_capacity(cfg, cand, stack_factory,
                                           P["valid"], store, funnel, on_candidate,
                                           symbol=primary)
        if cap_summary is None:
            continue
        # Stability neighbours evaluated on the validation slice.
        stab = await _gate_stability(cfg, P["dataset"], cand, stack_factory,
                                     P["valid"], store, funnel, on_candidate,
                                     symbol=primary)
        if stab is None:
            continue
        # Final score: primary full score blended with cross-market score
        # (single symbol -> primary score exactly, behaviour preserved).
        score, comps = research_score(
            valid_m, train_m, wf_res["folds"], stab["pass_rate"],
            stress.get("base_net", 0.0), stress.get("stressed_net", 0.0),
            concentration, cap_summary.get("max_survivable_capital"),
            cand.complexity)
        cross_score, cross_comps = _cross_score(secondary_details, valid_m)
        if len(symbols) > 1:
            blended = {**comps, "cross_market": cross_comps}
            final = 0.7 * score + 0.3 * cross_score
        else:
            blended = comps
            final = score
        cand.research_score = final
        cand.score_components = blended

        # Optional sealed-holdout confirmation (explicit mode + logged).
        primary_ds = splits[primary]["dataset"]
        primary_history = splits[primary]["train"] + splits[primary]["valid"]
        if cfg.allow_holdout and primary_ds.holdout is not None and len(primary_ds.holdout):
            hold = primary_ds.holdout.open_holdout(
                FINAL_MODE, cfg.campaign_id,
                f"final confirmation for {cand.candidate_id}")
            primary_ds.assert_not_holdout([], "noop")
            res, hm, _ = await _run_backtest(
                primary, hold,
                fam.build_strategy(cand.family, cand.params),
                _stack_factory_with(stack_factory), cfg.execution_model,
                warmup=primary_history[-cfg.warmup_bars:] if cfg.warmup_bars else None)
            hpass = int(hm.get('n_closed', 0)) >= 3 and hm.get('expectancy', 0.0) > 0
            cand.record_evaluation("final_holdout", hm, hpass,
                                   "" if hpass else "holdout_fail:nonpositive_expectancy")
            _persist_eval(store, cfg, cand, "final_holdout", hpass,
                          cand.rejection_reason, hm)
            if not hpass:
                funnel.setdefault("final_holdout", {"passed": 0, "rejected": 0})
                funnel["final_holdout"]["rejected"] += 1
                continue
            funnel.setdefault("final_holdout", {"passed": 0, "rejected": 0})
            funnel["final_holdout"]["passed"] += 1

        cand.record_evaluation(CandidateStatus.PAPER_CANDIDATE.value, valid_m, True, "")
        from research.mtesting import snooping_report
        mtest = snooping_report(cand.candidate_id,
                                list(getattr(valid_res, 'trade_pnls', []) or []),
                                n_trials=len(seen_exp_fp))
        _persist_eval(store, cfg, cand, CandidateStatus.PAPER_CANDIDATE.value,
                      True, "", {"research_score": score, **comps,
                                 "multiple_testing": mtest})
        store.upsert_experiment(cfg.campaign_id, cand.candidate_id,
                                cand.experiment_fingerprint,
                                CandidateStatus.PAPER_CANDIDATE.value, "completed")
        funnel[CandidateStatus.PAPER_CANDIDATE.value]["passed"] += 1
        promoted.append(cand.candidate_id)
        if on_candidate is not None:
            on_candidate(cand, "promoted")

    from research.candidates import _utcnow as _now
    from dataclasses import asdict as _asdict
    holdout_log = []
    for ds in ds_by_symbol.values():
        if ds.holdout is not None:
            holdout_log.extend({"campaign_id": r.campaign_id, "reason": r.reason,
                                "n_bars": r.n_bars, "timestamp": r.timestamp,
                                "symbol": ds.symbol}
                               for r in ds.holdout.access_log)
    record = {
        "campaign_id": cfg.campaign_id,
        "state": "completed",
        "symbol": primary,
        "symbols": symbols,
        "dataset_version": dataset_version,
        "dataset_versions": versions,
        "families": list(cfg.families),
        "candidate_limit": cfg.candidate_limit,
        "seed": cfg.seed,
        "funnel": funnel,
        "promoted": promoted,
        "started_at": started_at, "finished_at": _now().isoformat(),
        "gates": {k: getattr(cfg.gates, k) for k in vars(cfg.gates)},
        "allow_holdout": cfg.allow_holdout,
        "holdout_accesses": holdout_log,
        "optimizer": cfg.optimizer or 'random',
        "optimizer_trials": len(opt_trials),
        "protocol_version": eff_cfg["protocol_version"],
        "effective_config": eff_cfg,
    }
    store.save_campaign(record)
    return record


def _cross_score(secondary, primary_valid_m):
    """Blend helper: single market -> primary score untouched."""
    from research.scoring import cross_market_score
    per_symbol = {"primary": dict(primary_valid_m)}
    for sym, det in (secondary or {}).items():
        per_symbol[sym] = dict(det.get("valid_m", {}))
    return cross_market_score(per_symbol)


async def _confirm_secondary(cfg, cand, strategy, stack_factory, splits,
                             primary, store):
    """Cross-market confirmation on secondary symbols (no funnel bumps).

    Runs train/validation/walk-forward per secondary symbol and applies the
    cross-market gate over all symbols. Returns details dict or None.
    """
    from research.candidates import CandidateStatus as CS
    secondary = {}
    for sym in [s for s in campaign_symbols(cfg) if s != primary]:
        sp = splits[sym]
        ds = sp["dataset"]
        res_t, train_m, _ = await _run_backtest(
            sym, sp["train"], strategy,
            _stack_factory_with(stack_factory), cfg.execution_model, warmup=None)
        ok_t, reason_t = gates.gate_train(cand, train_m, cfg.gates)
        if not ok_t:
            payload = {"symbol": sym, "stage": "train",
                       "reason": reason_t, "metrics": train_m}
            cand.record_evaluation("cross_market", payload, False,
                                   f"{gates.REJECT_SINGLE_MARKET}:{sym}:train:{reason_t}")
            _persist_eval(store, cfg, cand, "cross_market", False,
                          cand.rejection_reason, payload)
            return None
        warmup = sp["train"][-cfg.warmup_bars:] if cfg.warmup_bars and sp["train"] else []
        res_v, valid_m, _ = await _run_backtest(
            sym, sp["valid"], strategy,
            _stack_factory_with(stack_factory), cfg.execution_model, warmup=warmup)
        ok_v, reason_v = gates.gate_validation(cand, train_m, valid_m, cfg.gates)
        if not ok_v:
            payload = {"symbol": sym, "stage": "validation",
                       "reason": reason_v, "metrics": valid_m}
            cand.record_evaluation("cross_market", payload, False,
                                   f"{gates.REJECT_SINGLE_MARKET}:{sym}:validation:{reason_v}")
            _persist_eval(store, cfg, cand, "cross_market", False,
                          cand.rejection_reason, payload)
            return None
        fold_metrics = []
        for fold in sp["wf_folds"]:
            _, fm, _ = await _run_backtest(
                sym, fold["test"], strategy,
                _stack_factory_with(stack_factory), cfg.execution_model,
                warmup=fold["warmup"])
            fold_metrics.append(fm)
        detail = {"symbol": sym, "train_m": train_m, "valid_m": valid_m,
                  "wf": fold_metrics, "valid_res": res_v, "valid": sp["valid"]}
        secondary[sym] = detail
        _persist_eval(store, cfg, cand, "cross_market", True, "",
                      {"symbol": sym, "valid": valid_m,
                       "wf_positive": sum(1 for f in fold_metrics
                                          if f.get("expectancy", 0.0) > 0),
                       "wf_folds": len(fold_metrics)})
    # Cross-market gate over primary + secondary validation metrics.
    per_symbol = {"primary": {"valid": dict(cand.evaluations[-1]["metrics"])}}
    for sym, det in secondary.items():
        per_symbol[sym] = {"valid": dict(det["valid_m"])}
    ok, reason = gates.gate_cross_market(cand, per_symbol, cfg.gates)
    payload = {"per_symbol": {s: {"valid_expectancy": per_symbol[s]["valid"].get("expectancy"),
                                  "valid_pf": per_symbol[s]["valid"].get("profit_factor"),
                                  "valid_dd": per_symbol[s]["valid"].get("max_drawdown"),
                                  "n_closed": per_symbol[s]["valid"].get("n_closed")}
                             for s in per_symbol},
               "secondary": {s: {"wf": d["wf"]} for s, d in secondary.items()}}
    if not ok:
        cand.record_evaluation("cross_market", payload, False, reason)
        store.save_evaluation(cand.candidate_id, "cross_market", False, reason, payload)
        store.save_candidate(cand)
        return None
    return {"details": secondary, "payload": payload}


def _make_holdout_access_cb(store, campaign_id: str, symbol: str,
                               external_cb=None):
    """Wrap a holdout access logger with access-time store persistence.

    Every FINAL_HOLDOUT opening is appended to the in-progress campaign
    record's `holdout_accesses` and saved immediately, so a process crash
    after the access (but before campaign end) cannot silently erase the
    event. The end-of-campaign assembly rewrites the same list from the
    seals' access logs, so no duplicate entries accumulate. Failures to
    persist are logged but never block research (fail-open on logging only;
    the in-memory seal log remains authoritative).
    """
    def _cb(rec):
        if external_cb is not None:
            try:
                external_cb(rec)
            except Exception:
                logger.warning("external holdout-access callback failed",
                               exc_info=True)
        try:
            record = store.load_campaign(campaign_id)
            if record is None:
                return
            log = list(record.get("holdout_accesses", []))
            log.append({"campaign_id": rec.campaign_id, "reason": rec.reason,
                        "n_bars": rec.n_bars, "timestamp": rec.timestamp,
                        "symbol": symbol})
            record["holdout_accesses"] = log
            store.save_campaign(record)
        except Exception:
            logger.warning("holdout access persistence failed", exc_info=True)
    return _cb


def _bump(funnel, stage, passed):
    funnel[stage]["passed" if passed else "rejected"] += 1


def _persist_eval(store, cfg, cand, stage, ok, reason, payload):
    """Single transactional write for candidate + evaluation + progress.

    Replaces the old save_evaluation/save_candidate pairs (which a crash
    could split). Marks the experiment completed iff this evaluation is
    terminally rejecting; successes stay in_progress until promotion.
    """
    from research.candidates import CandidateStatus as CS
    exp_fp = getattr(cand, 'experiment_fingerprint', None) or cand.fingerprint
    exp_state = "completed" if not ok else "in_progress"
    store.save_evaluation_bundle(cand, stage, ok, reason, payload,
                                 cfg.campaign_id, exp_stage=stage,
                                 exp_state=exp_state,
                                 exp_fingerprint=exp_fp)


def effective_config(cfg, ds_by_symbol, symbols):
    """Complete effective configuration for experiment identity + reports."""
    from research import RESEARCH_PROTOCOL_VERSION
    return {
        "protocol_version": RESEARCH_PROTOCOL_VERSION,
        "execution_model": cfg.execution_model,
        "cost_model": dict(BASE_COST_MODEL),
        "cost_multipliers": list(cfg.cost_multipliers or [1]),
        "capacity_multipliers": list(cfg.capacity_multipliers or [1]),
        "stability_neighbours": cfg.stability_neighbours,
        "splits": {"train_ratio": cfg.train_ratio,
                   "validation_ratio": cfg.validation_ratio,
                   "embargo_bars": cfg.embargo_bars,
                   "warmup_bars": cfg.warmup_bars,
                   "n_splits": cfg.n_splits,
                   "train_bars": cfg.train_bars,
                   "test_bars": cfg.test_bars},
        "holdout_ratios": {s: ds_by_symbol[s].holdout_ratio for s in symbols},
        "gates": {k: getattr(cfg.gates, k) for k in vars(cfg.gates)},
        "seed": cfg.seed,
        "families": sorted(cfg.families),
        "symbols": list(symbols),
        "datasets": {s: {"version": ds_by_symbol[s].version,
                         "content_hash": ds_by_symbol[s].content_hash,
                         "rows": len(ds_by_symbol[s].research_bars)
                         + len(ds_by_symbol[s].holdout)}
                     for s in symbols},
    }


def _experiment_fp(effective_cfg, strategy_fingerprint, seed):
    from research.candidates import experiment_fingerprint
    split_cfg = dict(effective_cfg["splits"])
    # The full cost configuration is part of experiment identity: the base
    # cost model AND the stress/capacity multiplier schedules. Two campaigns
    # that differ only in cost_multipliers evaluate different experiments.
    cost_cfg = dict(effective_cfg["cost_model"])
    cost_cfg["cost_multipliers"] = list(
        effective_cfg.get("cost_multipliers") or [1])
    cost_cfg["capacity_multipliers"] = list(
        effective_cfg.get("capacity_multipliers") or [1])
    return experiment_fingerprint(
        strategy_fingerprint,
        {s: d.get("content_hash") or d.get("version", "")
         for s, d in effective_cfg["datasets"].items()},
        split_cfg,
        max(effective_cfg["holdout_ratios"].values()),
        effective_cfg["execution_model"],
        cost_cfg,
        effective_cfg["gates"],
        effective_cfg["protocol_version"],
        seed)


def _is_complete(store, campaign_id, cand):
    """Authoritative completion check for resume.

    An explicit completed experiment row wins. Otherwise a terminal outcome
    (rejection or paper promotion) among this campaign's evaluations counts
    as complete. Anything else (including legacy untagged rows, which belong
    to pre-protocol runs) is re-derived conservatively below by the caller.
    """
    exp = store.get_experiment(campaign_id, cand.candidate_id)
    if exp is not None:
        return exp["state"] == "completed"
    evs = store.evaluations_for(cand.candidate_id, campaign_id)
    if not evs:
        # Pre-protocol databases tag nothing: fall back to untagged rows so
        # fully evaluated legacy candidates are still recognized.
        evs = store.evaluations_for(cand.candidate_id, "")
    return any((e["stage"] == "paper_candidate" and e["passed"]) or
               (not e["passed"] and e["stage"] not in ("optimizer_trial",))
               for e in evs)


async def _gate_train(cfg, dataset, cand, strategy, stack_factory, train,
                      store, funnel, on_candidate, symbol=None):
    dataset.assert_not_holdout(train, "train")
    sym = symbol or cfg.symbol
    res, m, _ = await _run_backtest(sym, train, strategy,
                                    _stack_factory_with(stack_factory),
                                    cfg.execution_model, warmup=None)
    ok, reason = gates.gate_train(cand, m, cfg.gates)
    cand.record_evaluation(CandidateStatus.TRAIN_EVALUATED.value, m, ok, reason)
    _persist_eval(store, cfg, cand, CandidateStatus.TRAIN_EVALUATED.value,
                  ok, reason, m)
    _bump(funnel, CandidateStatus.TRAIN_EVALUATED.value, ok)
    return ok


async def _gate_valid(cfg, dataset, cand, strategy, stack_factory,
                      train, valid, train_m, store, funnel, on_candidate,
                      symbol=None):
    dataset.assert_not_holdout(valid, "validation")
    sym = symbol or cfg.symbol
    warmup = train[-cfg.warmup_bars:] if cfg.warmup_bars and train else []
    res, m, _ = await _run_backtest(sym, valid, strategy,
                                    _stack_factory_with(stack_factory),
                                    cfg.execution_model, warmup=warmup)
    ok, reason = gates.gate_validation(cand, train_m, m, cfg.gates)
    cand.record_evaluation(CandidateStatus.VALIDATED.value, m, ok, reason)
    _persist_eval(store, cfg, cand, CandidateStatus.VALIDATED.value,
                  ok, reason, m)
    _bump(funnel, CandidateStatus.VALIDATED.value, ok)
    return (ok, res)


async def _gate_walk_forward(cfg, dataset, cand, stack_factory, wf_folds,
                             store, funnel, on_candidate, symbol=None):
    from research.candidates import CandidateStatus as CS
    sym = symbol or cfg.symbol
    fold_metrics, fold_details = [], []
    strategy = fam.build_strategy(cand.family, cand.params)
    for k, fold in enumerate(wf_folds):
        res, m, _ = await _run_backtest(
            sym, fold['test'], strategy,
            _stack_factory_with(stack_factory), cfg.execution_model,
            warmup=fold['warmup'])
        m['fold'] = k
        fold_metrics.append(m)
        fold_details.append({"fold": k, "metrics": m,
                             "test_start": fold['test'][0].timestamp.isoformat(),
                             "test_end": fold['test'][-1].timestamp.isoformat()})
    ok, reason = gates.gate_walk_forward(cand, fold_metrics, cfg.gates)
    agg = {"folds": fold_details,
           "pooled_expectancy": (sum(f.get('expectancy', 0.0) for f in fold_metrics)
                                 / len(fold_metrics)) if fold_metrics else 0.0}
    cand.record_evaluation(CS.WALK_FORWARD_PASSED.value, agg, ok, reason)
    _persist_eval(store, cfg, cand, CS.WALK_FORWARD_PASSED.value,
                  ok, reason, agg)
    _bump(funnel, CS.WALK_FORWARD_PASSED.value, ok)
    return {"folds": fold_metrics, "details": fold_details} if ok else None


async def _gate_costs(cfg, dataset, cand, strategy, stack_factory,
                      valid, store, funnel, on_candidate, symbol=None):
    from research.candidates import CandidateStatus as CS
    sym = symbol or cfg.symbol
    base = {'taker_fee': 0.002, 'maker_fee': 0.001,
            'spread_pct': 0.0005, 'slippage_pct': 0.0005}

    def cost_factory(costs):
        # Costs AND candidate strategy are installed here: overrides are
        # merged pre-construction (exchange/risk snapshot config at __init__),
        # and the candidate strategy replaces the default manager.
        return _stack_factory_with(stack_factory, strategy,
                                   extra=dict(costs))()

    from evaluation import run_cost_sweep
    levels = sorted({1.0} | {float(x) for x in (cfg.cost_multipliers or [1])})
    # The stressed comparison uses the MAXIMUM configured multiplier —
    # never a hardcoded scenario name. [1,2] stresses at 2x, [1,1.5,3] at
    # 3x, [1,1.5,2,3,5] at 5x. A single-level [1] run compares base to
    # itself (no stress available; the gate then checks base robustness).
    max_mult = max(levels)
    stressed_name = 'base' if max_mult == 1.0 else f"x{max_mult:g}"
    out = await run_cost_sweep(sym, valid, cost_factory,
                               [{'name': ('base' if m == 1.0 else f"x{m:g}"),
                                 **{k: v * m for k, v in base.items()}}
                                for m in levels],
                               execution_model=cfg.execution_model,
                               warmup_candles=None)
    by_name = {s['scenario']: s for s in out['scenarios']}
    base_m = by_name['base']
    stressed = by_name.get(stressed_name, base_m)
    ok, reason = gates.gate_cost_stress(cand, base_m, stressed, cfg.gates)
    payload = {"base_net": base_m.get('net_pnl', 0.0),
               "stressed_net": stressed.get('net_pnl', 0.0),
               "stressed_scenario": stressed_name,
               "stressed_multiplier": max_mult,
               "stressed_pf": stressed.get('profit_factor', 0.0),
               "matrix": [{"scenario": s["scenario"], "net": s.get('net_pnl', 0.0),
                           "profit_factor": s.get('profit_factor', 0.0),
                           "fees": s.get('fees_paid', 0.0),
                           "spread": s.get('spread_paid', 0.0),
                           "slippage": s.get('slippage_paid', 0.0),
                           "impact": s.get('impact_paid', 0.0),
                           "fills": s.get('fills', 0)}
                          for s in out['scenarios']],
               "scenarios": out['scenarios']}
    cand.record_evaluation(CS.COST_STRESS_PASSED.value, payload, ok, reason)
    _persist_eval(store, cfg, cand, CS.COST_STRESS_PASSED.value,
                  ok, reason, payload)
    _bump(funnel, CS.COST_STRESS_PASSED.value, ok)
    return ok


async def _gate_regime(cfg, cand, store, funnel, valid, valid_res, valid_m,
                       secondary=None):
    from research.candidates import CandidateStatus as CS
    attribution = attribute_trades(valid_res, valid)
    concentration = regime_concentration(attribution)
    per_symbol = {"primary": regime_report_table(attribution)}
    worst_conc = concentration
    # Hard gate per market: the configured concentration policy applies to
    # the primary market AND every secondary market. A concentrated
    # secondary must reject even when the primary is healthy (and vice
    # versa); the reason names the offending market.
    if concentration > cfg.gates.max_regime_concentration:
        reason = (f"{gates.REJECT_REGIME_DEPENDENT}:primary:"
                  f"concentration={concentration:.2f}")
        payload = {"attribution": attribution, "concentration": concentration,
                   "max_concentration_all_symbols": concentration,
                   "per_symbol": per_symbol,
                   "failed_market": "primary"}
        cand.record_evaluation(CS.REGIME_PASSED.value, payload, False, reason)
        _persist_eval(store, cfg, cand, CS.REGIME_PASSED.value,
                      False, reason, payload)
        _bump(funnel, CS.REGIME_PASSED.value, False)
        return None
    for sym, det in (secondary or {}).items():
        attr = attribute_trades(det["valid_res"], det["valid"])
        per_symbol[sym] = regime_report_table(attr)
        conc = regime_concentration(attr)
        worst_conc = max(worst_conc, conc)
        if conc > cfg.gates.max_regime_concentration:
            reason = (f"{gates.REJECT_REGIME_DEPENDENT}:secondary:{sym}:"
                      f"concentration={conc:.2f}")
            payload = {"attribution": attribution,
                       "concentration": concentration,
                       "max_concentration_all_symbols": worst_conc,
                       "per_symbol": per_symbol,
                       "failed_market": sym}
            cand.record_evaluation(CS.REGIME_PASSED.value, payload, False,
                                   reason)
            _persist_eval(store, cfg, cand, CS.REGIME_PASSED.value,
                          False, reason, payload)
            _bump(funnel, CS.REGIME_PASSED.value, False)
            return None
    regime_metrics = {"validation": valid_m}
    for regime, agg in attribution.items():
        n = int(agg.get("n", 0) or 0)
        pnl = float(agg.get("pnl", 0.0) or 0.0)
        regime_metrics[regime] = {"expectancy": (pnl / n) if n else 0.0,
                                  "gross_win": max(agg["pnl"], 0.0),
                                  "gross_loss": min(agg["pnl"], 0.0)}
    ok, reason = gates.gate_regime(cand, regime_metrics, cfg.gates)
    payload = {"attribution": attribution, "concentration": concentration,
               "max_concentration_all_symbols": worst_conc,
               "per_symbol": per_symbol}
    cand.record_evaluation(CS.REGIME_PASSED.value, payload, ok, reason)
    _persist_eval(store, cfg, cand, CS.REGIME_PASSED.value,
                  ok, reason, payload)
    _bump(funnel, CS.REGIME_PASSED.value, ok)
    return worst_conc if ok else None


async def _gate_capacity(cfg, cand, stack_factory, valid, store, funnel, on_candidate,
                         symbol=None):
    from research.candidates import CandidateStatus as CS
    from evaluation import estimate_capacity
    sym = symbol or cfg.symbol
    # Read base sizing from a throwaway default stack (no orders placed);
    # per-level stacks are then built pre-scaled so the exchange, risk and
    # portfolio constructors all observe the scaled capital.
    probe_cfg = stack_factory({})[0]
    try:
        base_cash = float(probe_cfg.get('initial_cash', 10000.0))
        base_risk = float(probe_cfg.get('risk_per_trade', 10.0))
    except (TypeError, ValueError):
        base_cash, base_risk = 10000.0, 10.0
    strategy = fam.build_strategy(cand.family, cand.params)

    def scale_factory(scale):
        out = stack_factory({'initial_cash': base_cash * scale,
                             'risk_per_trade': base_risk * scale})
        out[-1].strategy_manager = SingleStrategyManager(strategy)
        return out

    out = await estimate_capacity(sym, valid, scale_factory,
                                  cfg.capacity_multipliers,
                                  execution_model=cfg.execution_model)
    summary = out.get('summary', {})
    base_part = None
    for lv in out.get('levels', []):
        if abs(lv.get('multiplier', 0.0) - 1.0) < 1e-9:
            base_part = lv.get('max_participation', 0.0)
    ok, reason = gates.gate_capacity(
        cand, summary, {'max_participation': base_part or 0.0}, cfg.gates)
    payload = {"capacity_summary": summary, "levels": out.get('levels', [])}
    cand.record_evaluation(CS.CAPACITY_PASSED.value, payload, ok, reason)
    _persist_eval(store, cfg, cand, CS.CAPACITY_PASSED.value,
                  ok, reason, payload)
    _bump(funnel, CS.CAPACITY_PASSED.value, ok)
    return summary if ok else None


async def _gate_stability(cfg, dataset, cand, stack_factory, valid,
                          store, funnel, on_candidate, symbol=None):
    from research.candidates import REJECT_PARAM_UNSTABLE
    from research.stability import neighbourhood_report
    sym = symbol or cfg.symbol

    async def eval_fn(params):
        strategy = fam.build_strategy(cand.family, params)
        _, m, _ = await _run_backtest(sym, valid, strategy,
                                      _stack_factory_with(stack_factory),
                                      cfg.execution_model, warmup=None)
        return m

    rep = await neighbourhood_report(cand, eval_fn, cand.seed,
                                     n=cfg.stability_neighbours)
    ok = rep["pass_rate"] >= cfg.gates.min_stability_rate
    reason = "" if ok else f"{REJECT_PARAM_UNSTABLE}:pass_rate={rep['pass_rate']:.2f}"
    cand.record_evaluation("param_stability", rep, ok, reason)
    _persist_eval(store, cfg, cand, "param_stability", ok, reason, rep)
    funnel.setdefault("param_stability", {"passed": 0, "rejected": 0})
    _bump(funnel, "param_stability", ok)
    return rep if ok else None
