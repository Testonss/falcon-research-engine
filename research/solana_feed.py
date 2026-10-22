"""Read-only Jupiter Tokens V2 ingestion; fail closed on incomplete safety data."""
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from research.solana_hunter import TokenSnapshot, evaluate_token

BASE_URL = "https://api.jup.ag/tokens/v2/recent"


def parse_token(record, observed_at):
    """Do not invent missing safety, impact, or concentration fields."""
    if not isinstance(record, dict):
        raise ValueError("token must be an object")
    audit = record.get("audit")
    stats = record.get("stats5m")
    if not isinstance(audit, dict) or not isinstance(stats, dict):
        raise ValueError("missing audit or 5m stats")
    # API fields may change; fail closed if required values are absent.
    required = ("mintAuthorityDisabled", "freezeAuthorityDisabled", "topHoldersPercentage")
    if any(key not in audit for key in required):
        raise ValueError("missing required audit field")
    if not isinstance(audit["mintAuthorityDisabled"], bool) or not isinstance(audit["freezeAuthorityDisabled"], bool):
        raise ValueError("invalid authority flag")
    first_pool = record.get("firstPool", {})
    if not isinstance(first_pool, dict) or not first_pool.get("createdAt"):
        raise ValueError("missing first pool timestamp")
    created = datetime.fromisoformat(first_pool["createdAt"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("timezone required")
    age = (observed_at - created.timestamp()) / 60
    if age < 0:
        raise ValueError("future pool creation")
    # Price impact requires an actual route quote; never infer it from liquidity.
    return {
        "mint": record["id"],
        "observed_at": observed_at,
        "liquidity_usd": record["liquidity"],
        "volume_5m_usd": stats["buyVolume"] + stats["sellVolume"],
        "age_minutes": age,
        "top10_holder_pct": audit["topHoldersPercentage"],
        "mint_authority_enabled": not audit["mintAuthorityDisabled"],
        "freeze_authority_enabled": not audit["freezeAuthorityDisabled"],
        "price_impact_pct": None,
    }


def fetch_recent(api_key, timeout=10):
    if not api_key:
        raise ValueError("JUPITER_API_KEY required")
    req = Request(BASE_URL, headers={"x-api-key": api_key, "accept": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError("unexpected response")
    return payload


def inspect_recent(api_key=None, records=None, observed_at=None):
    """Read-only inspection. Never score/approve tokens without a route impact quote."""
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).timestamp()
    if records is None:
        records = fetch_recent(api_key or os.getenv("JUPITER_API_KEY"))
    output = []
    for item in records:
        try:
            data = parse_token(item, observed_at)
            # Explicitly refuse trading eligibility while route impact is unknown.
            output.append({"mint": data["mint"], "eligible": False,
                           "reason": "missing_route_price_impact",
                           "snapshot": data})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            output.append({"mint": item.get("id") if isinstance(item, dict) else None,
                           "eligible": False, "reason": "invalid_data",
                           "detail": str(exc)})
    return output
