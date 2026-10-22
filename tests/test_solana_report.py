import tempfile
import unittest
from pathlib import Path

from research.solana_replay import PriceObservation
from research.solana_report import build_report, load_observations, main


class SolanaReportTests(unittest.TestCase):
    def test_report_handles_unresolved(self):
        obs = [PriceObservation("A", 1000, 1), PriceObservation("A", 1300, 1.1),
               PriceObservation("B", 1000, 1)]
        report = build_report(obs)
        self.assertEqual(report["trade_count"], 1)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertIn("B", report["unresolved_exits"])

    def test_empty_trades_no_fake_win_rate(self):
        report = build_report([PriceObservation("A", 1000, 1)])
        self.assertIsNone(report["win_rate"])
        self.assertIsNone(report["profit_factor"])

    def test_csv_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "prices.csv"
            target = Path(d) / "report.json"
            source.write_text("mint,observed_at,price_usd\nA,1000,1\nA,1300,1.2\n", encoding="utf-8")
            self.assertEqual(len(load_observations(source)), 2)
            self.assertEqual(main(["--csv", str(source), "--out", str(target)]), 0)
            self.assertIn('"trade_count": 1', target.read_text(encoding="utf-8"))

    def test_reject_bad_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.csv"
            p.write_text("price\n1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_observations(p)


if __name__ == "__main__":
    unittest.main()
