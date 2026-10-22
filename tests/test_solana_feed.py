import unittest
from research.solana_feed import inspect_recent, parse_token


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "id": "examplemint", "liquidity": 100000,
            "firstPool": {"createdAt": "2023-11-14T22:00:00Z"},
            "stats5m": {"buyVolume": 12000, "sellVolume": 10000},
            "audit": {"mintAuthorityDisabled": True,
                      "freezeAuthorityDisabled": True,
                      "topHoldersPercentage": 32},
        }

    def test_read_only_no_trade_without_quote(self):
        result = inspect_recent(records=[self.item], observed_at=1700000000)
        self.assertFalse(result[0]["eligible"])
        self.assertEqual(result[0]["reason"], "missing_route_price_impact")

    def test_missing_audit_fails_closed(self):
        item = dict(self.item)
        item.pop("audit")
        self.assertEqual(inspect_recent(records=[item], observed_at=1700000000)[0]["reason"], "invalid_data")

    def test_missing_authority_field_fails_closed(self):
        item = dict(self.item)
        item["audit"] = {"topHoldersPercentage": 30}
        with self.assertRaises(ValueError):
            parse_token(item, 1700000000)

    def test_empty_api_key_rejected(self):
        from research.solana_feed import fetch_recent
        with self.assertRaises(ValueError):
            fetch_recent("")


if __name__ == "__main__":
    unittest.main()
