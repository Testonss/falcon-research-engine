"""
Metrics module for the Falcon Research Engine.
Lightweight in-memory collector (counters, gauges, trade stats).
"""

import time
import logging
from typing import Dict, Any, List
from dataclasses import dataclass, field
from collections import Counter

logger = logging.getLogger(__name__)


@dataclass
class TradeStat:
    symbol: str
    side: str
    quantity: float
    price: float
    timestamp: float
    success: bool
    blocked_by: str = ""


class MetricsCollector:
    """Simple metrics collector with no external dependencies."""

    def __init__(self):
        self.counters: Counter = Counter()
        self.gauges: Dict[str, float] = {}
        self.latencies: Dict[str, List[float]] = {}
        self.trades: List[TradeStat] = []
        self._start = time.time()

    def incr(self, name: str, amount: int = 1):
        self.counters[name] += amount

    def gauge(self, name: str, value: float):
        self.gauges[name] = float(value)

    def observe_latency(self, name: str, seconds: float):
        self.latencies.setdefault(name, []).append(float(seconds))
        if len(self.latencies[name]) > 1000:
            self.latencies[name] = self.latencies[name][-1000:]

    def record_trade(self, stat: TradeStat):
        self.trades.append(stat)
        self.incr('trades_total')
        if stat.success:
            self.incr('trades_success')
        else:
            self.incr('trades_blocked' if stat.blocked_by else 'trades_failed')

    def summary(self) -> Dict[str, Any]:
        lat_summary = {}
        for k, vals in self.latencies.items():
            if vals:
                lat_summary[k] = {'count': len(vals), 'avg': sum(vals) / len(vals),
                                  'max': max(vals), 'min': min(vals)}
        return {
            'uptime_s': time.time() - self._start,
            'counters': dict(self.counters),
            'gauges': dict(self.gauges),
            'latencies': lat_summary,
            'trades': len(self.trades),
        }


# Global default collector
default_metrics = MetricsCollector()
