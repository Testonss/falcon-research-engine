"""Reproducible offline Solana paper replay reporting (no real orders)."""
import csv
import json
from dataclasses import asdict
from pathlib import Path

from research.solana_replay import PriceObservation, replay_observations
from research.solana_paper import PaperCosts


def load_observations(path):
    """CSV must contain mint, observed_at (Unix seconds), price_usd."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"mint", "observed_at", "price_usd"} <= set(reader.fieldnames):
            raise ValueError("CSV requires mint,observed_at,price_usd")
        for row in reader:
            rows.append(PriceObservation(row["mint"], int(row["observed_at"]), float(row["price_usd"])))
    if not rows:
        raise ValueError("empty dataset")
    return rows


def build_report(observations, capital_usd=500.0, hold_seconds=300,
                 costs=PaperCosts()):
    result = replay_observations(observations, capital_usd, hold_seconds, costs)
    trades = result["trades"]
    wins = sum(t.pnl_usd > 0 for t in trades)
    losses = sum(t.pnl_usd < 0 for t in trades)
    gross_wins = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    gross_losses = -sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    return {
        "method": "independent_per_token_fixed_horizon_offline_replay",
        "capital_per_trade_usd": capital_usd,
        "holding_seconds": hold_seconds,
        "costs": asdict(costs),
        "trades": [asdict(t) for t in trades],
        "trade_count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) if trades else None,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "mean_return_pct": sum(t.return_pct for t in trades) / len(trades) if trades else None,
        "unresolved_exits": result["unresolved_exits"],
        "unresolved_count": result["unresolved_count"],
        "limitations": [
            "not a shared-capital portfolio or executable trading strategy",
            "entries are first observed price, not a proven predictive signal",
            "missing exits are unresolved and may be total losses",
            "no liquidity, MEV, failed-swap or route availability model",
            "prices must be point-in-time observations including dead tokens",
        ],
    }


def write_report(report, path):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                           encoding="utf-8")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Falcon Solana offline paper replay")
    parser.add_argument("--csv", required=True, help="Point-in-time price observation CSV")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--capital", type=float, default=500.0)
    parser.add_argument("--hold-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    report = build_report(load_observations(args.csv), args.capital, args.hold_seconds)
    write_report(report, args.out)
    print(f"trades={report['trade_count']} unresolved={report['unresolved_count']} win_rate={report['win_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
