"""
Test suite for the Falcon Research Engine.
Run with: python -m unittest discover -s tests -v
"""

import unittest

from tests.test_portfolio import TestPortfolioManager  # noqa: F401
from tests.test_risk import TestRiskManager  # noqa: F401
from tests.test_strategy import TestStrategy  # noqa: F401
from tests.test_exchange import TestPaperExchange  # noqa: F401
from tests.test_market_data import TestMarketData  # noqa: F401
from tests.test_execution import TestExecution  # noqa: F401
from tests.test_backtest import TestBacktest  # noqa: F401
from tests.test_backtest_accounting import TestForensicAccounting  # noqa: F401
from tests.test_evaluation import (  # noqa: F401
    TestCSVLoader, TestExecutionModel, TestSplits, TestMetrics,
    TestWalkForwardRunner, TestWarmup, TestDataset, TestFoldIsolation,
    TestRegimes, TestCostSweep, TestModelComparison, TestExport,
)
from tests.test_liquidity import (  # noqa: F401
    TestParticipationCap, TestImpactModel, TestDynamicSpread,
    TestCostBreakdown, TestCapacity,
)
from tests.test_research import (  # noqa: F401
    TestGeneration, TestRegistry, TestSealedHoldout, TestGates,
    TestScoring, TestRegimes, TestStability, TestPersistence,
    TestPaperIdempotency, TestCampaignTiny, TestCLISmoke,
    TestIncrementalEMA, TestDatafiles, TestMultisymbol, TestCostMatrix,
    TestMtesting, TestInterfaces, TestPaperDivergence, TestRegimeMetrics,
    TestOptimizer, TestDatasetCLI, TestFreshHoldoutTail,
)

if __name__ == '__main__':
    unittest.main()
