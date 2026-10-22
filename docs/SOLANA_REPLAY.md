# Solana offline chronological replay

The research module `research/solana_replay.py` evaluates price observations per token, with a fixed holding horizon and the explicit round-trip costs from `research/solana_paper.py`.

This is **not** a trading strategy, continuous paper broker, wallet tracker, live API client, or profit forecast. It deliberately requires point-in-time observations rather than historical survivor-only listings. Tokens with no observed exit are listed in `unresolved_exits` and never counted as winning trades. Each token uses independent notional capital: **do not sum PnL as if a shared portfolio executed these trades**.

Example (synthetic data):

```python
from research.solana_replay import PriceObservation, replay_observations
rows = [PriceObservation("TOKEN", 1000, 1.0), PriceObservation("TOKEN", 1300, 1.2)]
print(replay_observations(rows))
```

Limitations: replay assumes entry/exit fills are possible and ignores liquidity evolution, stale prices, token taxes, sandwich/MEV, route failures and halted exits. An unresolved exit may represent a total loss, not merely missing data. Validate against a real historical dataset including failed tokens before claiming any edge.
