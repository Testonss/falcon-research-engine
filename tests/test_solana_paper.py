import unittest
from research.solana_paper import PaperCosts, replay_round_trip


class SolanaPaperTests(unittest.TestCase):
    def test_flat_price_loses_after_costs(self):
        result = replay_round_trip(500, 1, 1)
        self.assertLess(result.net_pnl_usd, 0)

    def test_positive_price_change_not_guaranteed_profit(self):
        result = replay_round_trip(500, 1, 1.01)
        self.assertLess(result.net_pnl_usd, 0)

    def test_zero_costs_are_neutral(self):
        result = replay_round_trip(500, 1, 1, PaperCosts(0, 0, 0, 0, 0, 0))
        self.assertAlmostEqual(result.net_pnl_usd, 0)

    def test_price_drop(self):
        self.assertLess(replay_round_trip(500, 1, 0.5).net_return_pct, -50)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            replay_round_trip(500, 0, 1)
        with self.assertRaises(ValueError):
            replay_round_trip(500, 1, float("nan"))

    def test_cost_validation(self):
        with self.assertRaises(ValueError):
            replay_round_trip(500, 1, 1, PaperCosts(buy_slippage_bps=10000))


if __name__ == "__main__":
    unittest.main()
