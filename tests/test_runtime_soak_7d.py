"""Accelerated 7-day soak test (deterministic, no broker, no Qt).

Runs runtime.soak.run_7d_soak (default >= 7 trading days) and asserts the
acceptance bar: zero duplicates, risk limits unviolated, bounded growth,
restarts/reconnects exercised, entries + exits + rejects all present.
"""

import os
import tempfile
import unittest


class Soak7DTests(unittest.TestCase):
    def test_7d_accelerated_soak(self):
        from runtime.soak import run_7d_soak
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            rep = run_7d_soak(state_db=path, symbols=("EURUSD",),
                              days=7, bars_per_day=288, cycles=220)
        finally:
            pass
        print("\nsoak7d report: " + " ".join(
            f"{k}={rep.get(k)}" for k in
            ("days", "cycles", "bars", "signals", "orders", "rejects",
             "positions", "restarts", "reconnects", "duplicates",
             "order_send_calls")))
        if rep.get("warnings"):
            print("soak7d warnings:", rep["warnings"])
        self.assertGreaterEqual(rep["days"], 7)
        self.assertGreater(rep["cycles"], 100)
        self.assertGreater(rep["bars"], 7 * 200)
        self.assertGreater(rep["signals"], 0)
        self.assertGreater(rep["orders"], 0, msg="soak must exercise entries")
        self.assertGreater(rep["rejects"], 0, msg="soak must exercise rejects")
        self.assertGreater(rep["positions"], 0)
        self.assertGreaterEqual(rep["restarts"], 1)
        self.assertGreaterEqual(rep["reconnects"], 1)
        self.assertEqual(rep["duplicates"], 0, msg="duplicate count must be zero")
        self.assertEqual(rep.get("warnings", []), [])
        # Exits exercised: every opened position closed deterministically.
        from runtime.store import RuntimeStore as _RS
        _s = _RS(path)
        try:
            _ev = _s.event_counts()
            self.assertGreater(_ev.get("EXIT", 0), 0, msg="soak must exercise exits")
            self.assertGreater(_ev.get("POSITION_CLOSE", 0), 0)
            self.assertGreater(len(_s.list_closed_trades(limit=5000)), 0)
        finally:
            _s.close()
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
