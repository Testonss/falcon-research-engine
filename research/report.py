"""Campaign reporting: machine-readable JSON, CSV rows, terminal summary."""
from typing import Dict, Any, List

from evaluation import export_report_json, export_folds_csv


def build_campaign_report(record: Dict[str, Any], store=None) -> Dict[str, Any]:
    """Assemble a full report from a campaign record (+ store for details).

    Strict campaign isolation: only candidates with at least one evaluation
    row tagged with this campaign_id are included. Pre-protocol databases
    (untagged rows, no protocol_version on the record) fall back to the
    legacy all-candidates view so old reports keep rendering.
    """
    cands = []
    if store is not None:
        dicts = store.list_campaign_candidates(record.get("campaign_id", ""))
        if not dicts and record.get("protocol_version") is None:
            dicts = [d for d in store.list_candidates()
                     if store.evaluations_for(d["candidate_id"])]
        for d in dicts:
            evs = store.evaluations_for(d["candidate_id"], record.get("campaign_id", ""))
            if not evs and record.get("protocol_version") is None:
                evs = store.evaluations_for(d["candidate_id"])
            cands.append({
                "candidate_id": d["candidate_id"],
                "family": d.get("family"), "params": d.get("params"),
                "status": d.get("status"), "stage": d.get("stage"),
                "complexity": d.get("complexity"),
                "research_score": d.get("research_score"),
                "score_components": d.get("score_components", {}),
                "rejection_reason": d.get("rejection_reason", ""),
                "rejection_stage": d.get("rejection_stage", ""),
                "parent_id": d.get("parent_id"),
                "evaluations": [{"stage": e["stage"], "passed": e["passed"],
                                 "reason": e["reason"],
                                 "metrics": e.get("metrics", {})} for e in evs],
            })
    cands.sort(key=lambda c: (c.get("research_score") is None,
                              -(c.get("research_score") or 0.0)))
    return {
        "campaign_id": record.get("campaign_id"),
        "symbol": record.get("symbol"),
        "symbols": record.get("symbols", [record.get("symbol")]),
        "dataset_version": record.get("dataset_version"),
        "dataset_versions": record.get("dataset_versions", {}),
        "seed": record.get("seed"),
        "families": record.get("families"),
        "funnel": record.get("funnel", {}),
        "promoted": record.get("promoted", []),
        "candidates": cands,
        "gates": record.get("gates", {}),
        "holdout_accesses": record.get("holdout_accesses", []),
        "optimizer": record.get("optimizer", "random"),
        "optimizer_trials": record.get("optimizer_trials", 0),
    }


def terminal_summary(report: Dict[str, Any]) -> str:
    """Concise human-readable funnel + survivors."""
    lines = [f"Falcon campaign {report.get('campaign_id')} "
             f"({report.get('symbol')}, {report.get('dataset_version')})"]
    funnel = report.get("funnel", {})
    lines.append(f"generated={funnel.get('generated', 0)} "
                 f"dup_skipped={funnel.get('duplicates_skipped', 0)} "
                 f"optimizer={report.get('optimizer', 'random')} "
                 f"trials={report.get('optimizer_trials', 0)}")
    order = ["train_evaluated", "validated", "cross_market",
             "walk_forward_passed", "cost_stress_passed", "regime_passed",
             "capacity_passed", "paper_candidate"]
    chain = []
    for stage in order:
        f = funnel.get(stage, {})
        chain.append(f"{stage}:{f.get('passed', 0)}ok/{f.get('rejected', 0)}rej")
    lines.append("funnel: " + " -> ".join(chain))
    promoted = report.get("promoted", [])
    lines.append(f"promoted={len(promoted)} {promoted}")
    for c in report.get("candidates", []):
        if c.get("status") == "paper_candidate":
            lines.append(f"  SURVIVOR {c['candidate_id']} {c['family']} "
                         f"{c['params']} score={c.get('research_score')}")
    rejected = [c for c in report.get("candidates", []) if c.get("rejection_reason")]
    by_reason: Dict[str, int] = {}
    for c in rejected:
        by_reason[c["rejection_reason"].split(":")[0]] = \
            by_reason.get(c["rejection_reason"].split(":")[0], 0) + 1
    lines.append("rejections: " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    holdout = report.get("holdout_accesses", [])
    lines.append(f"holdout_accesses={len(holdout)}"
                 + (f" {[(h.get('campaign_id'), h.get('reason')) for h in holdout]}"
                    if holdout else " (sealed)"))
    return "\n".join(lines)


def write_campaign_reports(report: Dict[str, Any], prefix: str):
    """Write <prefix>.json (+ <prefix>_candidates.csv). Returns paths."""
    jp = export_report_json(prefix + ".json", report)
    import csv
    cp = prefix + "_candidates.csv"
    fields = ["candidate_id", "family", "params", "status", "stage",
              "complexity", "research_score", "rejection_reason",
              "rejection_stage", "parent_id"]
    with open(cp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for c in report.get("candidates", []):
            row = dict(c)
            row["params"] = str(row.get("params"))
            w.writerow(row)
    return jp, cp
