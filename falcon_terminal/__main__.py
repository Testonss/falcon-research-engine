"""Composition root for the native terminal (QML primary, Tk fallback).

The Tk prototype stays intact as the safe fallback (`--ui tk`).
"""

import argparse
import logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Falcon Terminal: read-only native live market terminal")
    parser.add_argument("--ui", default="qml", choices=("qml", "tk"),
                        help="presentation layer (default: qml; tk is the fallback)")
    parser.add_argument("--provider", default="both",
                        choices=("both", "binance", "solana", "mt5"),
                        help="initial watchlist scope (default: both)")
    parser.add_argument("--symbol", default="BTCUSDT",
                        help="Binance spot symbol (default BTCUSDT)")
    parser.add_argument("--mint", default=None,
                        help="Solana token mint (default: wrapped SOL)")
    parser.add_argument("--solana-mint", default=None,
                        help="alias of --mint (kept for compatibility)")
    parser.add_argument("--mt5", action="store_true",
                        help="enable MT5 DEMO integration (demo only; real blocked)")
    parser.add_argument("--mt5-path", default=None,
                        help="optional MT5 terminal path (default: attach to running terminal)")
    parser.add_argument("--page", default="radar",
                        choices=("radar", "signals", "meme", "assets"))
    parser.add_argument("--poll-interval", type=float, default=15.0,
                        help="Solana poll seconds, minimum 2")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 2 <= args.poll_interval <= 3600:
        parser.error("--poll-interval must be between 2 and 3600 seconds")
    if args.ui == "tk":
        return _run_tk(parser, args)
    return _run_qml(parser, args)


def _run_qml(parser, args):
    try:
        from falcon_terminal.qml_main import main as qml_main
    except ImportError as exc:
        parser.exit(1, f"QML terminal unavailable ({exc}). "
                       'Install it with: pip install ".[terminal]" '
                       "or retry with --ui tk (no extra dependencies).\n")
    passthrough = ["--provider", args.provider, "--symbol", args.symbol,
                   "--page", args.page,
                   "--poll-interval", str(args.poll_interval)]
    mint = args.mint or args.solana_mint
    if mint:
        passthrough += ["--mint", mint]
    if args.mt5 or args.provider == "mt5":
        passthrough += ["--mt5"]
    if args.mt5_path:
        passthrough += ["--mt5-path", args.mt5_path]
    return qml_main(passthrough)


def _run_tk(parser, args):
    try:
        import tkinter as tk
        from falcon_terminal.app import TerminalApp
    except ImportError:
        parser.exit(1, "Tkinter is required. Install Python with Tcl/Tk support (Linux: python3-tk).\n")
    from falcon_terminal.bridge import TerminalBridge
    mint = args.mint or args.solana_mint or \
        "So11111111111111111111111111111111111111112"
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        parser.exit(1, f"Cannot open native desktop window: {exc}\n")
    _ = args.provider  # watchlist always carries both feeds; selection is in-app
    bridge = TerminalBridge(symbol=args.symbol, solana_mint=mint,
                            poll_interval=args.poll_interval)
    app = TerminalApp(root, bridge, symbol=args.symbol, solana_mint=mint)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.close()
        if bridge.is_alive:
            root.mainloop()
    finally:
        bridge.stop()
        bridge.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
