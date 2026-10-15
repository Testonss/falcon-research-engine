"""Canonical asset / instrument identity model.

CEX identity and on-chain identity are different things. Ticker strings
must never be used as universal primary keys: two different Solana tokens
can share the same symbol, and one CEX symbol string can mean different
things on different venues.

Rules:
- CEX instruments are identified by (venue, base, quote).
- On-chain assets are identified by (chain, mint). The mint address is
  authoritative; symbol/name are display metadata only.
- Pools are identified by (chain, dex, pool_address).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


@dataclass(frozen=True)
class MarketInstrument:
    """A fungible spot market on a centralized venue."""

    venue: str
    base: str
    quote: str
    symbol: str  # display form, e.g. "BTC/USDT"

    def key(self) -> str:
        return f"{self.venue}:{self.base}/{self.quote}"


@dataclass(frozen=True)
class OnChainAsset:
    """A token on a chain. The mint address is the identity."""

    chain: str
    mint: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    decimals: Optional[int] = None

    def key(self) -> str:
        return f"{self.chain}:{self.mint}"


@dataclass(frozen=True)
class OnChainPool:
    """A liquidity pool / market for an on-chain asset."""

    chain: str
    dex: str
    pool_address: str
    base_mint: str
    quote_mint: str
    quote_symbol: Optional[str] = None

    def key(self) -> str:
        return f"{self.chain}:{self.dex}:{self.pool_address}"


def validate_solana_mint(mint: str) -> str:
    """Validate a Solana mint address; return the normalized (stripped) form.

    Raises ValueError on any malformed input. Never touches the network.
    """
    if not isinstance(mint, str):
        raise ValueError(f"mint must be a string, got {type(mint).__name__}")
    m = mint.strip()
    if not m:
        raise ValueError("mint address is empty")
    if len(m) < 32 or len(m) > 44:
        raise ValueError(f"invalid mint length {len(m)} (expected 32..44 chars)")
    bad = [c for c in m if c not in _BASE58_ALPHABET]
    if bad:
        raise ValueError(f"mint address is not base58 (bad chars: {bad[:4]!r})")
    # A real Solana address decodes to exactly 32 bytes.
    try:
        n = _decode_base58(m)
    except ValueError as e:
        raise ValueError(f"mint address is not valid base58: {e}")
    if len(n) != 32:
        raise ValueError(f"mint address decodes to {len(n)} bytes, expected 32")
    return m


def _decode_base58(s: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    idx = {c: i for i, c in enumerate(alphabet)}
    num = 0
    for c in s:
        if c not in idx:
            raise ValueError(f"bad base58 char {c!r}")
        num = num * 58 + idx[c]
    # Count leading '1's -> leading zero bytes.
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * pad + raw


def is_valid_solana_mint(mint: object) -> bool:
    try:
        validate_solana_mint(mint)  # type: ignore[arg-type]
        return True
    except (ValueError, TypeError):
        return False


_QUOTE_RE = re.compile(r"^[A-Z0-9]{2,15}$")


def normalize_venue_symbol(base: str, quote: str) -> str:
    b = (base or "").strip().upper()
    q = (quote or "").strip().upper()
    if not b or not q:
        raise ValueError("base and quote must both be non-empty")
    if not _QUOTE_RE.match(b) or not _QUOTE_RE.match(q):
        raise ValueError(f"invalid base/quote: {base!r}/{quote!r}")
    return f"{b}/{q}"
