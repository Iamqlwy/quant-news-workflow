"""LimitTracker 测试。"""

from unittest.mock import MagicMock

import pandas as pd

from src.market.data.cache import CacheManager
from src.market.services.limit import LimitTracker


class TestLimitPct:
    """涨跌停比例判定测试。"""

    def _make_tracker(self, stock_basic: pd.DataFrame | None = None) -> LimitTracker:
        cache = CacheManager()
        cache.session.stock_basic = stock_basic
        clock = MagicMock()
        bar_svc = MagicMock()
        return LimitTracker(cache, bar_svc, clock, "/tmp/klines")

    def test_gem_20pct(self) -> None:
        """创业板 30 开头 → 20%。"""
        tracker = self._make_tracker()
        assert tracker._get_limit_pct_from_prefix("300001.SZ") == 0.20

    def test_star_20pct(self) -> None:
        """科创板 68 开头 → 20%。"""
        tracker = self._make_tracker()
        assert tracker._get_limit_pct_from_prefix("688001.SH") == 0.20

    def test_bse_30pct(self) -> None:
        """北交所 92 开头 → 30%。"""
        tracker = self._make_tracker()
        assert tracker._get_limit_pct_from_prefix("920001.BJ") == 0.30

    def test_main_10pct(self) -> None:
        """主板 00/60 开头 → 10%。"""
        tracker = self._make_tracker()
        assert tracker._get_limit_pct_from_prefix("000001.SZ") == 0.10
        assert tracker._get_limit_pct_from_prefix("600000.SH") == 0.10

    def test_st_5pct_from_basic(self) -> None:
        """主板 ST 股 → 5%。"""
        basic = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "name": ["*ST平安"],
        })
        tracker = self._make_tracker(stock_basic=basic)
        assert tracker._get_limit_pct("000001.SZ") == 0.05

    def test_gem_st_keeps_20pct(self) -> None:
        """创业板 ST 股涨跌停保持 20%。"""
        basic = pd.DataFrame({
            "ts_code": ["300001.SZ"],
            "name": ["*ST某某"],
            "market": ["GEM"],
        })
        tracker = self._make_tracker(stock_basic=basic)
        assert tracker._get_limit_pct("300001.SZ") == 0.20

    def test_star_st_keeps_20pct(self) -> None:
        """科创板 ST 股涨跌停保持 20%。"""
        basic = pd.DataFrame({
            "ts_code": ["688001.SH"],
            "name": ["*ST某某"],
            "market": ["STAR"],
        })
        tracker = self._make_tracker(stock_basic=basic)
        assert tracker._get_limit_pct("688001.SH") == 0.20

    def test_bse_st_keeps_30pct(self) -> None:
        """北交所 ST 股涨跌停保持 30%。"""
        basic = pd.DataFrame({
            "ts_code": ["920001.BJ"],
            "name": ["*ST某某"],
            "market": ["BSE"],
        })
        tracker = self._make_tracker(stock_basic=basic)
        assert tracker._get_limit_pct("920001.BJ") == 0.30

    def test_gem_from_basic(self) -> None:
        """创业板从 stock_basic 获取 → 20%。"""
        basic = pd.DataFrame({
            "ts_code": ["300001.SZ"],
            "name": ["某创业板"],
            "market": ["GEM"],
        })
        tracker = self._make_tracker(stock_basic=basic)
        assert tracker._get_limit_pct("300001.SZ") == 0.20

    def test_fallback_when_no_basic(self) -> None:
        """stock_basic 缺失时回退到前缀匹配。"""
        tracker = self._make_tracker(stock_basic=None)
        assert tracker._get_limit_pct("300001.SZ") == 0.20
        assert tracker._get_limit_pct("000001.SZ") == 0.10
