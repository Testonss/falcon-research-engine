import unittest
from research.solana_replay import PriceObservation, replay_observations
from research.solana_paper import PaperCosts


class ReplayTests(unittest.TestCase):
    def test_chronological_fixed_horizon(self):
        obs = [PriceObservation("A", 1100, 1.5), PriceObservation("A", 1000, 1),
               PriceObservation("A", 1400, 2), PriceObservation("A", 1200, 100)]
        result = replay_observations(obs, hold_seconds=300, costs=PaperCosts(0, 0, 0, 0, 0, 0))
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0].exit_at, 1400)
        self.assertAlmostEqual(result["trades"][0].return_pct, 100)

    def test_missing_exit_not_a_win(self):
        result = replay_observations([PriceObservation("A", 1000, 1)])
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["unresolved_exits"], ["A"])

    def test_duplicate_time_rejected(self):
        with self.assertRaises(ValueError):
            replay_observations([PriceObservation("A", 1000, 1), PriceObservation("A", 1000, 2)])

    def test_multiple_tokens_independent(self):
        obs = [PriceObservation("B", 1000, 1), PriceObservation("B", 1300, 1),
               PriceObservation("A", 1000, 1), PriceObservation("A", 1300, 1)]
        self.assertEqual(replay_observations(obs)["trade_count"], 2)

    def test_invalid_horizon(self):
        with self.assertRaises(ValueError):
            replay_observations([], hold_seconds=0)


if __name__ == "__main__":
    unittest.main()
