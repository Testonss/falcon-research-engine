import unittest
from dataclasses import replace
from research.solana_hunter import TokenSnapshot, evaluate_token


class HunterTests(unittest.TestCase):
    def setUp(self):
        self.s = TokenSnapshot("mint", 1700000000, 100000, 25000, 60, 35, 0.5, False, False)

    def test_eligible(self):
        d = evaluate_token(self.s)
        self.assertTrue(d.eligible)
        self.assertGreater(d.score, 0)

    def test_authority_block(self):
        self.assertIn("freeze_authority", evaluate_token(replace(self.s, freeze_authority_enabled=True)).reasons)

    def test_low_liquidity(self):
        self.assertFalse(evaluate_token(replace(self.s, liquidity_usd=100)).eligible)

    def test_invalid_values(self):
        self.assertEqual(evaluate_token(replace(self.s, price_impact_pct=float("nan"))).reasons, ("invalid_snapshot",))

    def test_no_probability_claim(self):
        self.assertLessEqual(evaluate_token(self.s).score, 1)


if __name__ == "__main__":
    unittest.main()
