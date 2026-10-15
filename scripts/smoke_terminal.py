"""Opt-in live desktop smoke: python scripts/smoke_terminal.py --duration 50 --out smoke.json."""

from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from falcon_terminal.app import TerminalApp  # noqa: E402
from falcon_terminal.bridge import TerminalBridge  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=50)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.duration < 35:
        parser.error("--duration must be at least 35 seconds to observe another Solana poll")

    started = time.monotonic()
    elapsed = lambda: round(time.monotonic() - started, 3)
    history = deque(maxlen=240)
    stats, errors, checked_selection = {}, [], set()
    result = {"requested_duration_s": args.duration, "mapped_s": None,
              "first_render_s": None, "first_data_s": {}, "shutdown_timeout": False}
    root = bridge = app = None

    def on_render(snapshot):
        now = elapsed()
        if result["first_render_s"] is None:
            result["first_render_s"] = now
        selected = app.market.selection()
        detail = {key: var.get() for key, var in app.detail.items()}
        rows = {}
        for asset in snapshot.assets:
            row = tuple(str(value) for value in app.market.item(asset.key, "values"))
            rows[asset.key] = row
            entry = stats.setdefault(asset.key, {
                "provider": asset.provider, "first_messages": None, "last_messages": 0,
                "prices": set(), "quotes": set(), "source": set(), "feeds": set(),
                "healthy_seen": False, "detail_matches": 0,
            })
            messages = int(row[3])
            entry["last_messages"] = messages
            entry["feeds"].add(row[2])
            if messages > 0:
                if entry["first_messages"] is None:
                    entry["first_messages"] = messages
                    result["first_data_s"][asset.key] = now
                if row[1] != "—":
                    entry["prices"].add(row[1])
                entry["healthy_seen"] |= asset.connected and not asset.stale
            if selected and selected[0] == asset.key:
                matches = (detail["key"] == asset.key and detail["price"] == row[1]
                           and detail["symbol"] == asset.symbol
                           and detail["quote"] == asset.quote and detail["feed"] == row[2]
                           and detail["source"] == f"{asset.provider} · {asset.source}")
                if not matches:
                    errors.append(f"Selected detail mismatch: {asset.key} at {now}s")
                elif messages > 0:
                    checked_selection.add(asset.key)
                    entry["detail_matches"] += 1
                    entry["source"].add(detail["source"])
                    if detail["bid"] != "—" and detail["ask"] != "—":
                        entry["quotes"].add((detail["bid"], detail["ask"]))
        history.append({"elapsed_s": now, "rows": rows, "selected": selected,
                        "detail": detail, "status": app.status.get()})

    def mapped(event):
        if event.widget == root and result["mapped_s"] is None:
            result["mapped_s"] = elapsed()

    def select_next():
        if app.closing:
            return
        keys = app.market.get_children()
        if keys:
            selected = app.market.selection()
            index = (keys.index(selected[0]) + 1) % len(keys) if selected else 0
            app.market.selection_set(keys[index])
        root.after(6000, select_next)

    def stop():
        result["close_requested_s"] = elapsed()
        app.close()
        if bridge.is_alive:
            root.after(15000, force_stop)

    def force_stop():
        result["shutdown_timeout"] = True
        bridge.stop()
        root.destroy()

    try:
        root = tk.Tk()
        root.bind("<Map>", mapped, add="+")
        root.report_callback_exception = lambda kind, value, tb: errors.append(
            f"Tk callback failed: {kind.__name__}: {value}")
        bridge = TerminalBridge()
        app = TerminalApp(root, bridge, on_render=on_render)
        root.after(6000, select_next)
        root.after(int(args.duration * 1000), stop)
        root.mainloop()
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if bridge is not None:
            bridge.stop()
            bridge.join(timeout=5)
        result["worker_stopped"] = bridge is not None and not bridge.is_alive
        result["duration_s"] = elapsed()

    checks = {
        "native_window_mapped": result["mapped_s"] is not None,
        "shell_mapped_within_3s": result["mapped_s"] is not None and result["mapped_s"] < 3,
        "completed_requested_observation": result["duration_s"] >= args.duration,
        "both_assets_present": len(stats) == 2,
        "both_selected_details_match": len(checked_selection) == 2,
        "clean_shutdown": result["worker_stopped"] and not result["shutdown_timeout"],
        "no_errors": not errors,
    }
    for key, entry in stats.items():
        prefix = entry["provider"].lower()
        checks[f"{prefix}_feed_advanced"] = (entry["first_messages"] is not None
                                            and entry["last_messages"] > entry["first_messages"])
        checks[f"{prefix}_usable_price_and_health"] = bool(entry["prices"]) and entry["healthy_seen"]
        if "binance" in prefix:
            checks["binance_bid_ask_rendered"] = bool(entry["quotes"])
            checks["binance_rendered_values_changed"] = len(entry["prices"]) > 1 or len(entry["quotes"]) > 1
        for field in ("prices", "quotes", "source", "feeds"):
            entry[field] = sorted(entry[field])
    result.update(checks=checks, assets=stats, history=list(history), errors=errors,
                  verdict="PASS" if all(checks.values()) else "FAIL")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "checks": checks,
                      "report": str(args.out.resolve())}, indent=2))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
