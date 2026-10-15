"""Release-polish regressions: quiet default CLI, verbose opt-in, branding.

Proves routine simulation events (risk blocks, fills, connects) stay
silent at default log level while real errors still surface, and that
public branding is Falcon (legacy aliases documented, not advertised).
"""
import io
import logging
import unittest
from contextlib import redirect_stderr


NOISY_LOGGERS = ("risk", "execution", "market_data", "exchange",
                 "backtest", "portfolio", "evaluation")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TestQuietCLI(unittest.TestCase):
    def test_default_backtest_is_quiet_but_errors_surface(self):
        import scripts.falcon as falcon
        root = logging.getLogger()
        cap = _Capture()
        old_level = root.level
        root.addHandler(cap)
        try:
            code = falcon.main(["backtest",
                                "--strategy-json", "examples/strategy.json",
                                "--data", "data/sample_ohlcv.csv"])
            self.assertEqual(code, 0)
        finally:
            root.removeHandler(cap)
            root.setLevel(old_level)
        noisy = [r for r in cap.records
                 if r.name.split(".")[0] in NOISY_LOGGERS
                 and r.levelno >= logging.WARNING]
        self.assertEqual(
            noisy, [],
            "default CLI must not emit WARNING+ simulation chatter: %s"
            % [f"{r.name}:{r.getMessage()[:80]}" for r in noisy[:5]])
        # Real errors still surface (non-zero exit + stderr text).
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                falcon.main(["backtest",
                             "--strategy-json", "does-not-exist.json",
                             "--data", "data/sample_ohlcv.csv"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("falcon: error:", err.getvalue())

    def test_verbose_and_log_level_flags(self):
        import scripts.falcon as falcon
        from types import SimpleNamespace
        root = logging.getLogger()
        old_level = root.level
        try:
            self.assertEqual(
                falcon.configure_logging(
                    SimpleNamespace(verbose=False, log_level=None)),
                "WARNING")
            self.assertEqual(root.level, logging.WARNING)
            self.assertEqual(
                falcon.configure_logging(
                    SimpleNamespace(verbose=True, log_level=None)),
                "INFO")
            self.assertEqual(root.level, logging.INFO)
            self.assertEqual(
                falcon.configure_logging(
                    SimpleNamespace(verbose=False, log_level="DEBUG")),
                "DEBUG")
            self.assertEqual(root.level, logging.DEBUG)
        finally:
            root.setLevel(old_level)

    def test_verbose_status_end_to_end(self):
        import scripts.falcon as falcon
        import tempfile
        import os
        root = logging.getLogger()
        old_level = root.level
        try:
            with tempfile.TemporaryDirectory() as d:
                code = falcon.main(["--verbose", "status", "--db",
                                    os.path.join(d, "v.db")])
                self.assertEqual(code, 0)
                self.assertLessEqual(root.level, logging.INFO)
        finally:
            root.setLevel(old_level)


class TestBranding(unittest.TestCase):
    def test_public_docs_have_no_scalping_branding(self):
        import re
        for path in ("README.md", "AGENTS.md", "CONTRIBUTING.md",
                     "RELEASE_NOTES_v0.1.0.md", "ROADMAP.md"):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            hits = re.findall(r"(?i)scalping(\s*bot)?", src)
            self.assertEqual(hits, [], f"{path} still has legacy branding")

    def test_module_docstrings_have_no_scalping_branding(self):
        import importlib
        for mod in ("bot_logging", "config", "execution", "backtest",
                    "market_data", "exchange", "paper", "portfolio",
                    "metrics", "risk", "strategy"):
            doc = importlib.import_module(mod).__doc__ or ""
            self.assertNotIn("scalping", doc.lower(), mod)

    def test_legacy_aliases_documented(self):
        import bot_logging
        import main
        self.assertIs(bot_logging.ScalpingBotLogger, bot_logging.FalconLogger)
        self.assertIs(main.ScalpingBot, main.FalconPaperApp)
        with open("bot_logging/__init__.py", encoding="utf-8") as f:
            self.assertIn("Legacy alias", f.read())
        with open("main.py", encoding="utf-8") as f:
            self.assertIn("Legacy alias", f.read())


if __name__ == "__main__":
    unittest.main()
