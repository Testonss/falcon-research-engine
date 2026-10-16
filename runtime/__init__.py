"""Autonomous MT5 DEMO runtime v0.2 (headless, H24-capable).

Headless autonomous loop over MT5 DEMO only. REAL-money execution is
structurally forbidden: every mutating path goes through
:class:`execution.mt5_demo.DemoExecutionAdapter`, whose double DEMO guard
(trade_mode == 0 checked at enable AND before every order_send) is kept
intact here. This package never calls ``mt5.order_send`` directly.

Modules:
  features  deterministic closed-bar feature calculation (pure)
  signals   deterministic signal generation (pure, explicit bar timestamp)
  sizing    deterministic risk-based position sizing (pure)
  decision  decision engine: signal -> risk gate -> sizing -> action
  execution anti-duplicate DEMO execution path wrapping DemoExecutionAdapter
  reconcile position reconciliation (local intent vs terminal truth)
  store     SQLite persistence + crash recovery
  health    heartbeat + health snapshot
  config    runtime configuration (env + ConfigManager overlay)
  runtime   headless autonomous loop (pause/resume/recovery)
"""

from runtime.config import RuntimeConfig
from runtime.runtime import AutonomousDemoRuntime

__all__ = ["RuntimeConfig", "AutonomousDemoRuntime"]
