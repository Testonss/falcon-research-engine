"""Composition root for the optional native terminal."""

import argparse
import logging


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Falcon Terminal: read-only native live market watchlist")
    parser.add_argument("--provider", default="both", choices=("both", "binance", "solana"),
                        help="initial watchlist scope (default: both)")
    parser.add_argument("--symbol", default="BTCUSDT", help="Binance spot symbol (default BTCUSDT)")
    parser.add_argument("--mint", default=None,
                        help="Solana token mint (default: wrapped SOL)")
    parser.add_argument("--solana-mint", default=None,
                        help="alias of --mint (kept for compatibility)")
    parser.add_argument("--poll-interval", type=float, default=15.0, help="Solana poll seconds, minimum 2")
    args = parser.parse_args(argv)
    if not 2 <= args.poll_interval <= 3600:
        parser.error("--poll-interval must be between 2 and 3600 seconds")
    mint = args.mint or args.solana_mint or "So11111111111111111111111111111111111111112"
    try:
        import tkinter as tk
        from falcon_terminal.app import TerminalApp
    except ImportError:
        parser.exit(1, "Tkinter is required. Install Python with Tcl/Tk support (Linux: python3-tk).\n")
    from falcon_terminal.bridge import TerminalBridge
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
