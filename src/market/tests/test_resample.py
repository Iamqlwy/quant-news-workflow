"""resample 测试。"""

import pandas as pd

from src.market.compute.resample import resample_bars


class TestResampleBars:
    def test_resample_5m(self) -> None:
        # 09:31~09:40 跨越 3 个 5 分钟窗口: 09:30(4根), 09:35(5根), 09:40(1根)
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-16 09:31", periods=10, freq="1min"),
            "open": [10.0] * 10,
            "high": [10.5] * 10,
            "low": [9.5] * 10,
            "close": [10.2] * 10,
            "volume": [100.0] * 10,
            "amount": [1000.0] * 10,
        })
        result = resample_bars(df, "5m")
        assert len(result) == 3

    def test_resample_weekly_fri_anchor(self) -> None:
        """周线应以周五为基准对齐。"""
        # 模拟一周的交易数据
        dates = pd.date_range("2025-01-13", periods=5, freq="B")  # Mon-Fri
        df = pd.DataFrame({
            "timestamp": dates,
            "open": [10.0, 10.1, 10.2, 10.3, 10.4],
            "high": [10.5, 10.6, 10.7, 10.8, 10.9],
            "low": [9.5, 9.6, 9.7, 9.8, 9.9],
            "close": [10.1, 10.2, 10.3, 10.4, 10.5],
            "volume": [100.0] * 5,
            "amount": [1000.0] * 5,
        })
        result = resample_bars(df, "1w")
        assert len(result) == 1  # 同一交易周应聚合成 1 根周线
        assert result.iloc[0]["close"] == 10.5

    def test_resample_empty(self) -> None:
        df = pd.DataFrame()
        result = resample_bars(df, "5m")
        assert result.empty

    def test_resample_unknown_granularity(self) -> None:
        df = pd.DataFrame({"timestamp": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "amount": []})
        result = resample_bars(df, "3m")
        assert result.empty
