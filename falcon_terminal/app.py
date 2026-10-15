"""Native view: only immutable bridge DTOs cross the UI boundary."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Protocol

from falcon_terminal.bridge import TerminalBridge
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot


class SnapshotBridge(Protocol):
    def start(self) -> None: ...
    def poll(self) -> Optional[TerminalSnapshot]: ...
    def stop(self) -> None: ...
    @property
    def is_alive(self) -> bool: ...


BG = "#0d131c"
PANEL = "#151e2b"
TEXT = "#e5edf6"
MUTED = "#93a5bb"
ACCENT = "#59dfb3"
DOWN = "#e5484d"
WARNING = "#f2c078"


def number(value: Optional[float]) -> str:
    """Human price/quote formatting. Never invents precision."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    if v == 0 or abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.10f}".rstrip("0").rstrip(".")


def compact_money(value: Optional[float]) -> str:
    """$1.83M / $73.40B style sizing for liquidity/cap/FDV. None -> em dash."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v) or v < 0:
        return "—"
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if v >= threshold:
            return f"${v / threshold:,.2f}{suffix}"
    return f"${v:,.2f}"


def change_text(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def feed_label(asset: AssetSnapshot) -> str:
    if asset.stale:
        return f"{asset.state} / STALE" if asset.state != "STALE" else "STALE"
    if asset.connected and asset.messages == 0:
        return "WAITING FOR DATA"
    return asset.state


class TerminalApp:
    """Tk stays on the main thread; network work never runs in callbacks."""

    def __init__(self, root: tk.Tk, bridge: SnapshotBridge,
                 on_render: Optional[Callable[[TerminalSnapshot], None]] = None,
                 symbol: str = "BTCUSDT",
                 solana_mint: str = "",
                 bridge_factory: Optional[Callable[[str, str], SnapshotBridge]] = None):
        self.root, self.bridge = root, bridge
        self.on_render = on_render
        self.snapshot: Optional[TerminalSnapshot] = None
        self.render_count = 0
        self.closing = False
        self._timer: Optional[str] = None
        self._startup: Optional[str] = None
        self._switch_after: Optional[str] = None
        self._pending_targets: Optional[tuple] = None
        self._targets = (symbol, solana_mint)
        self._bridge_factory = bridge_factory or (
            lambda s, m: TerminalBridge(symbol=s, solana_mint=m))
        self._last_chart: tuple = ()
        self.detail: dict[str, tk.StringVar] = {}
        root.title("Falcon Terminal · Live Market")
        root.geometry("1240x760")
        root.minsize(1080, 680)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        # Paint the shell before starting provider bootstrap.
        from falcon_terminal import perf as _perf
        self._t_app = _perf.now()
        _perf.global_tracker.mark("app_start")
        self._startup = root.after_idle(self._start)

    def _label(self, parent, text="", *, size=10, color=TEXT, bold=False,
               variable=None, **kwargs):
        return tk.Label(parent, text=text, textvariable=variable, bg=PANEL,
                         fg=color, font=("Segoe UI", size, "bold" if bold else "normal"),
                         anchor="w", **kwargs)

    def _build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=48, borderwidth=0,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background="#1d2a3a", foreground=MUTED,
                        relief="flat", padding=(8, 12), font=("Segoe UI", 9))
        style.map("Treeview", background=[("selected", "#243d4d")],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#243d4d")])
        header = tk.Frame(self.root, bg=BG, padx=24, pady=20)
        header.pack(fill="x")
        tk.Label(header, text="FALCON  /  TERMINAL", bg=BG, fg=TEXT,
                 font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Label(header, text="LIVE DATA   ·   READ ONLY", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side="right")

        body = tk.Frame(self.root, bg=BG, padx=24)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=6)
        body.columnconfigure(1, weight=5)
        body.rowconfigure(0, weight=1)
        market_panel = tk.Frame(body, bg=PANEL, padx=18, pady=18)
        market_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._label(market_panel, "Radar / Market", size=15, bold=True).pack(fill="x")
        self._label(market_panel, "Live watchlist · select an asset", color=MUTED).pack(
            fill="x", pady=(4, 18))
        self.market = ttk.Treeview(market_panel, columns=("asset", "price", "feed", "events"),
                                   show="headings", selectmode="browse", height=5)
        for key, title, width in (("asset", "ASSET / QUOTE", 130),
                                  ("price", "PRICE", 115), ("feed", "FEED", 125),
                                  ("events", "MESSAGES", 85)):
            self.market.heading(key, text=title, anchor="w")
            self.market.column(key, width=width, minwidth=65, anchor="w")
        self.market.tag_configure("fresh", foreground=ACCENT)
        self.market.tag_configure("waiting", foreground=WARNING)
        self.market.pack(fill="both", expand=True)
        self.market.bind("<<TreeviewSelect>>", self._selection_changed)
        self._label(market_panel, "Messages count received feed updates, including unchanged polls.",
                    color=MUTED, size=9, wraplength=480).pack(fill="x", pady=(14, 0))
        self._label(market_panel, "Solana: aggregated pool snapshots; bid/ask unavailable.",
                    color=MUTED, size=9, wraplength=480).pack(fill="x", pady=(6, 0))
        # Manual market selection (no token discovery in this prototype).
        self._label(market_panel, "MARKET SELECTION", color=MUTED, size=9).pack(
            fill="x", pady=(14, 2))
        form = tk.Frame(market_panel, bg=PANEL)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        tk.Label(form, text="Binance", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.symbol_var = tk.StringVar(value=self._targets[0])
        tk.Entry(form, textvariable=self.symbol_var, bg="#0d131c", fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 font=("Consolas", 10)).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        tk.Label(form, text="Solana mint", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mint_var = tk.StringVar(value=self._targets[1])
        tk.Entry(form, textvariable=self.mint_var, bg="#0d131c", fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 font=("Consolas", 9)).grid(row=1, column=1, sticky="ew",
                                            padx=(8, 0), pady=(6, 0))
        self.apply_btn = tk.Button(form, text="Apply", bg="#1d2a3a", fg=TEXT,
                                   activebackground="#243d4d", activeforeground=TEXT,
                                   relief="flat", font=("Segoe UI", 9, "bold"),
                                   command=self._apply_targets)
        self.apply_btn.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(8, 0))

        selected = tk.Frame(body, bg=PANEL, padx=22, pady=18)
        selected.grid(row=0, column=1, sticky="nsew")
        for name in ("symbol", "source", "price", "quote", "feed", "change", "bid", "ask",
                     "spread", "liquidity", "volume", "buys", "sells", "candle",
                     "candle_state", "age", "received", "pool", "key"):
            self.detail[name] = tk.StringVar(value="—")
        self.detail["symbol"].set("Waiting for market data")
        self._label(selected, "SELECTED ASSET", color=MUTED, size=9).pack(fill="x")
        self._label(selected, variable=self.detail["symbol"], size=21, bold=True).pack(fill="x", pady=(4, 0))
        self._label(selected, variable=self.detail["source"], color=MUTED, size=9).pack(fill="x", pady=(2, 12))
        price_row = tk.Frame(selected, bg=PANEL)
        price_row.pack(fill="x")
        self._label(price_row, variable=self.detail["price"], size=30, bold=True).pack(side="left")
        self._label(price_row, variable=self.detail["quote"], color=MUTED, size=11).pack(side="left", padx=12, pady=(18, 0))
        self.change_widget = self._label(price_row, variable=self.detail["change"], size=11, bold=True)
        self.change_widget.pack(side="left", padx=4, pady=(18, 0))
        self.feed_widget = self._label(selected, variable=self.detail["feed"], color=WARNING, size=10, bold=True)
        self.feed_widget.pack(fill="x", pady=(4, 8))
        # Live price history (real sampled prices only; empty until observed).
        self._label(selected, "LIVE PRICE", color=MUTED, size=8).pack(fill="x")
        self.chart = tk.Canvas(selected, bg="#0d131c", highlightthickness=0, height=110)
        self.chart.pack(fill="x", pady=(2, 10))
        metrics = tk.Frame(selected, bg=PANEL)
        metrics.pack(fill="x")
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)
        for index, (name, title) in enumerate((("bid", "BID"), ("ask", "ASK"),
                    ("spread", "SPREAD"), ("change", "24H CHANGE"),
                    ("liquidity", "LIQUIDITY · USD"), ("volume", "VOLUME 5m · USD"),
                    ("buys", "BUYS · 5m"), ("sells", "SELLS · 5m"))):
            cell = tk.Frame(metrics, bg=PANEL, pady=6)
            cell.grid(row=index // 2, column=index % 2, sticky="ew")
            self._label(cell, title, size=8, color=MUTED).pack(fill="x")
            self._label(cell, variable=self.detail[name], size=14).pack(fill="x")
        self._label(selected, "CURRENT CANDLE", color=MUTED, size=8).pack(fill="x", pady=(8, 0))
        self._label(selected, variable=self.detail["candle"], size=11).pack(fill="x")
        self._label(selected, variable=self.detail["candle_state"], color=MUTED, size=9).pack(fill="x")
        self._label(selected, variable=self.detail["age"], color=MUTED, size=9).pack(fill="x", pady=(12, 2))
        self._label(selected, variable=self.detail["received"], color=MUTED, size=9).pack(fill="x")
        self._label(selected, variable=self.detail["pool"], color=MUTED, size=8,
                    wraplength=430).pack(fill="x", pady=(8, 6))
        tk.Entry(selected, textvariable=self.detail["key"], state="readonly", relief="flat",
                 readonlybackground=PANEL, fg=MUTED, font=("Consolas", 8)).pack(fill="x")

        status_panel = tk.Frame(self.root, bg=PANEL, padx=18, pady=14)
        status_panel.pack(fill="x", padx=24, pady=14)
        self._label(status_panel, "Signal / Status", size=12, bold=True).pack(fill="x")
        self._label(status_panel, "Signal engine not connected · no signals generated · execution disabled",
                    color=MUTED).pack(fill="x", pady=(4, 8))
        self.status = tk.StringVar(value="Opening live feeds…")
        self._label(status_panel, variable=self.status, size=9, wraplength=1130).pack(fill="x")
        self.footer = tk.StringVar(value="Falcon Research Engine  /  desktop prototype")
        tk.Label(self.root, textvariable=self.footer, bg=BG, fg=MUTED,
                 font=("Consolas", 9), anchor="w").pack(fill="x", padx=24, pady=(0, 12))

    def _start(self):
        self._startup = None
        if self.closing:
            return
        from falcon_terminal import perf as _perf
        _perf.global_tracker.measure("startup_first_frame", self._t_app,
                                     "Tk shell painted")
        self.bridge.start()
        self._tick()

    def _tick(self):
        self._timer = None
        if self.closing:
            return
        snapshot = self.bridge.poll()
        if snapshot is not None:
            self.render(snapshot)
        if not self.bridge.is_alive:
            self.status.set("Backend stopped. Close and reopen the terminal to reconnect.")
            return
        self._timer = self.root.after(100, self._tick)

    def render(self, snapshot: TerminalSnapshot):
        self.snapshot = snapshot
        self.render_count += 1
        keys = {asset.key for asset in snapshot.assets}
        for key in self.market.get_children():
            if key not in keys:
                self.market.delete(key)
        for asset in snapshot.assets:
            name = asset.symbol if "/" in asset.symbol else f"{asset.symbol} · {asset.quote}"
            values = (name, number(asset.price),
                      feed_label(asset), str(asset.messages))
            tag = "fresh" if asset.connected and not asset.stale and asset.messages else "waiting"
            if self.market.exists(asset.key):
                self.market.item(asset.key, values=values, tags=(tag,))
            else:
                self.market.insert("", "end", iid=asset.key, values=values, tags=(tag,))
        if not self.market.selection() and snapshot.assets:
            self.market.selection_set(snapshot.assets[0].key)
        self._selection_changed()
        summaries = [f"{a.provider}: {feed_label(a)}" + (f" · {a.error}" if a.error else "")
                     for a in snapshot.assets]
        self.status.set("\n".join(summaries))
        self.footer.set(f"AUTO UPDATE  ·  snapshot {snapshot.sequence}  ·  {snapshot.generated_at}  ·  public data only")
        if self.on_render:
            self.on_render(snapshot)

    def _selection_changed(self, _event=None):
        if self.snapshot is None or not self.market.selection():
            return
        key = self.market.selection()[0]
        asset = next((a for a in self.snapshot.assets if a.key == key), None)
        if asset is None:
            return
        if asset.change_pct_24h is None:
            change_color = MUTED
        else:
            change_color = ACCENT if asset.change_pct_24h >= 0 else DOWN
        self.change_widget.configure(fg=change_color)
        if asset.candle_close is None:
            candle_line = "—"
            candle_state = "Candle unavailable for this feed"
        else:
            candle_line = (f"O {number(asset.candle_open)}  H {number(asset.candle_high)}  "
                           f"L {number(asset.candle_low)}  C {number(asset.candle_close)}  "
                           f"V {number(asset.candle_volume)}")
            candle_state = (f"{asset.candle_interval or '—'} · "
                            f"{'CLOSED' if asset.candle_closed else 'FORMING — provisional, not history'}")
        fields = {"symbol": asset.symbol, "source": f"{asset.provider} · {asset.source}",
                  "price": number(asset.price), "quote": asset.quote, "feed": feed_label(asset),
                  "change": change_text(asset.change_pct_24h),
                  "bid": number(asset.bid), "ask": number(asset.ask),
                  "spread": "—" if asset.spread_bps is None else f"{asset.spread_bps:.1f} bps",
                  "liquidity": compact_money(asset.liquidity_usd),
                  "volume": compact_money(asset.volume_5m),
                  "buys": "—" if asset.buys_5m is None else str(asset.buys_5m),
                  "sells": "—" if asset.sells_5m is None else str(asset.sells_5m),
                  "candle": candle_line, "candle_state": candle_state,
                  "age": "No feed message received yet" if asset.age_s is None else f"Last feed message: {asset.age_s:.1f}s ago",
                  "received": f"Received (UTC): {asset.observed_at or '—'}",
                  "pool": f"Pool: {asset.dex or '—'} / {asset.pool or '—'}", "key": asset.key}
        for name, value in fields.items():
            self.detail[name].set(value)
        self.feed_widget.configure(fg=ACCENT if asset.connected and not asset.stale and asset.messages else WARNING)
        self._draw_chart(asset.price_history)

    def _draw_chart(self, history: tuple):
        if history == self._last_chart:
            return
        self._last_chart = history
        canvas = self.chart
        canvas.delete("all")
        try:
            width = max(50, canvas.winfo_width())
        except Exception:
            width = 400
        height = 110
        if len(history) < 2:
            canvas.create_text(width // 2, height // 2, text="waiting for price history",
                               fill=MUTED, font=("Segoe UI", 9))
            return
        lo, hi = min(history), max(history)
        span = hi - lo if hi > lo else abs(hi) or 1.0
        n = len(history)
        points = []
        for i, v in enumerate(history):
            x = 4 + (width - 8) * (i / (n - 1))
            y = height - 8 - (height - 16) * ((v - lo) / span)
            points.extend((x, y))
        rising = history[-1] >= history[0]
        canvas.create_line(*points, fill=ACCENT if rising else DOWN, width=1.5)

    def _apply_targets(self):
        if self.closing or self._pending_targets is not None:
            return
        symbol = (self.symbol_var.get() or "").strip().upper() or self._targets[0]
        mint = (self.mint_var.get() or "").strip() or self._targets[1]
        if (symbol, mint) == self._targets:
            return
        self._pending_targets = (symbol, mint)
        self.status.set(f"Switching markets to {symbol} / {mint[:8]}… — stopping current feeds.")
        self.bridge.stop()
        self._switch_after = self.root.after(50, self._finish_switch)

    def _finish_switch(self):
        self._switch_after = None
        if self.closing:
            return
        if self.bridge.is_alive:
            self._switch_after = self.root.after(50, self._finish_switch)
            return
        symbol, mint = self._pending_targets or self._targets
        self._pending_targets = None
        self._targets = (symbol, mint)
        # Clear stale asset-specific state before the new feeds publish.
        for key in self.market.get_children():
            self.market.delete(key)
        for var in self.detail.values():
            var.set("—")
        self.detail["symbol"].set("Waiting for market data")
        self._last_chart = ()
        self.chart.delete("all")
        self.snapshot = None
        self.status.set("Opening live feeds…")
        self.bridge = self._bridge_factory(symbol, mint)
        self.bridge.start()

    def close(self):
        if self.closing:
            return
        self.closing = True
        for callback in (self._startup, self._timer, self._switch_after):
            if callback is not None:
                try:
                    self.root.after_cancel(callback)
                except Exception:
                    pass
        self.status.set("Closing feeds and waiting for pending requests…")
        self.footer.set("STOPPING  ·  clean shutdown in progress")
        self.bridge.stop()
        self._await_stop()

    def _await_stop(self):
        if self.bridge.is_alive:
            self.root.after(50, self._await_stop)
        else:
            self.root.destroy()
