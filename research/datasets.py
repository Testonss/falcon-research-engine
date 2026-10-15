"""Sealed dataset protocol: TRAIN / VALIDATION / WALK-FORWARD / FINAL_HOLDOUT.

The FINAL_HOLDOUT is sealed at build time. Research code only ever receives
non-holdout slices; opening the holdout requires the explicit mode "final"
plus a reason, and every access is logged. Tests prove the research path
never touches it.

Holdout access policy (A — sealed during research, explicitly openable in
final mode):
- During research the holdout is never opened (every research slice is
  checked with `assert_not_holdout`, which raises on leakage).
- In final evaluation mode (`mode="final"` + non-empty reason) the holdout
  MAY be opened multiple times — once per promoted candidate when the
  campaign allows it. Every opening is appended to the seal's access log
  (and, inside campaigns, persisted to the campaign record at access
  time). This is policy A, NOT one-time semantics: repeated final-mode
  opens are permitted and each one is logged.
- What `holdout_access_count == 0` proves: no FINAL_HOLDOUT bars flowed
  through research slices or final confirmations inside this process.
  What it does NOT prove: that the underlying CSV on disk was never read
  out-of-band (e.g. by a separate process opening the file directly).
"""
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger(__name__)

FINAL_MODE = "final"


@dataclass
class HoldoutAccess:
    campaign_id: str
    reason: str
    n_bars: int
    timestamp: str


class HoldoutSeal:
    """Sealed FINAL_HOLDOUT container. Access is explicit + logged."""

    def __init__(self, candles, version: str,
                 on_access: Optional[Callable[[HoldoutAccess], None]] = None):
        self._candles = list(candles)
        self.version = version
        self.access_log: List[HoldoutAccess] = []
        self._on_access = on_access

    def __len__(self) -> int:
        return len(self._candles)

    @property
    def sealed(self) -> bool:
        return True

    def set_access_logger(self, callback) -> None:
        """Attach (or replace) the access-log sink. Used by campaigns."""
        self._on_access = callback

    @property
    def start_ts(self):
        return min(c.timestamp for c in self._candles) if self._candles else None

    def open_holdout(self, mode: str, campaign_id: str, reason: str):
        """Return holdout bars only in explicit final-evaluation mode."""
        if mode != FINAL_MODE:
            raise PermissionError(
                f"Holdout access denied: mode must be {FINAL_MODE!r}, got {mode!r}")
        if not reason:
            raise ValueError("Holdout access requires a non-empty reason")
        from research.candidates import _utcnow
        rec = HoldoutAccess(campaign_id=campaign_id, reason=reason,
                            n_bars=len(self._candles),
                            timestamp=_utcnow().isoformat())
        self.access_log.append(rec)
        if self._on_access is not None:
            self._on_access(rec)
        logger.warning("FINAL_HOLDOUT opened by %s (%s, %d bars)",
                       campaign_id, reason, len(self._candles))
        return list(self._candles)


@dataclass
class ResearchDataset:
    version: str
    symbol: str
    research_bars: list = field(default_factory=list)
    holdout: Optional[HoldoutSeal] = None
    source: str = ""
    holdout_ratio: float = 0.15
    # Manifest sha256 of the underlying versioned file when the bars were
    # loaded through load_versioned_dataset ("" for ad-hoc/test data).
    content_hash: str = ""

    def assert_not_holdout(self, candles, label: str = "input") -> None:
        """Raise if any bar belongs to the sealed holdout period."""
        if not self.holdout or not len(self.holdout):
            return
        start = self.holdout.start_ts
        bad = [c for c in candles if c.timestamp >= start]
        if bad:
            raise AssertionError(
                f"{label}: {len(bad)} bars inside sealed FINAL_HOLDOUT (leakage)")

    def attach_access_logger(self, callback) -> None:
        """Sink every holdout access into an external log (campaign record)."""
        if self.holdout is not None:
            self.holdout.set_access_logger(callback)


def dataset_version(symbol: str, n_bars: int, first_ts, last_ts, source: str) -> str:
    payload = f"{symbol}|{n_bars}|{first_ts}|{last_ts}|{source}"
    return "ds_" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def build_research_dataset(candles, symbol: str, holdout_ratio: float = 0.15,
                           source: str = "",
                           on_holdout_access=None,
                           content_hash: str = "") -> ResearchDataset:
    """Split sorted candles into research (past) + sealed FINAL_HOLDOUT (tail)."""
    bars = sorted(candles, key=lambda c: c.timestamp)
    if not 0.0 < holdout_ratio < 0.5:
        raise ValueError(f"holdout_ratio must be in (0, 0.5), got {holdout_ratio}")
    if len(bars) < 20:
        raise ValueError(f"Need >= 20 bars, got {len(bars)}")
    n_hold = max(5, int(len(bars) * holdout_ratio))
    research, hold = bars[:-n_hold], bars[-n_hold:]
    version = dataset_version(symbol, len(bars), bars[0].timestamp,
                              bars[-1].timestamp, source)
    return ResearchDataset(
        version=version, symbol=symbol, research_bars=research,
        holdout=HoldoutSeal(hold, version, on_access=on_holdout_access),
        source=source, holdout_ratio=holdout_ratio, content_hash=content_hash)
