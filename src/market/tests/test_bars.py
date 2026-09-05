"""Bar 合成和查找测试。"""

import pandas as pd

from src.market.compute.bars import build_daily_bar_from_1m, enrich_live_bar, find_previous_close


class TestBuildDailyBarFrom1m:
    def test_basic(self) -> None:
        df_1m = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2025-01-15 09:30:00", "2025-01-15 09:31:00", "2025-01-15 09:32:00",
                "2025-01-16 09:30:00", "2025-01-16 09:31:00",
            ]),
            "open": [10.0, 10.1, 10.2, 10.5, 10.6],
            "high": [10.1, 10.2, 10.3, 10.7, 10.8],
            "low": [9.9, 10.0, 10.1, 10.4, 10.5],
            "close": [10.1, 10.2, 10.3, 10.6, 10.7],
            "volume": [100, 200, 300, 50, 60],
            "amount": [1000, 2000, 3000, 500, 600],
        })

        result = build_daily_bar_from_1m(df_1m, "20250115")
        assert result is not None
        assert len(result) == 1
        assert result["open"].iloc[0] == 10.0
        assert result["high"].iloc[0] == 10.3
        assert result["low"].iloc[0] == 9.9
        assert result["close"].iloc[0] == 10.3
        assert result["volume"].iloc[0] == 600  # 100+200+300
        assert result["amount"].iloc[0] == 6000  # 1000+2000+3000

    def test_no_today_data(self) -> None:
        df_1m = pd.DataFrame({
            "timestamp": pd.to_datetime(["2025-01-14 09:30:00"]),
            "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.1],
            "volume": [100], "amount": [1000],
        })
        result = build_daily_bar_from_1m(df_1m, "20250115")
        assert result is None

    def test_empty_df(self) -> None:
        df_1m = pd.DataFrame()
        result = build_daily_bar_from_1m(df_1m, "20250115")
        assert result is None


class TestFindPreviousClose:
    def test_basic(self) -> None:
        daily = pd.DataFrame({
            "timestamp": pd.to_datetime(["2025-01-10", "2025-01-13", "2025-01-14"]),
            "close": [10.0, 10.5, 11.0],
        })
        result = find_previous_close(daily, "2025-01-15")
        assert result == 11.0

    def test_same_day_excluded(self) -> None:
        daily = pd.DataFrame({
            "timestamp": pd.to_datetime(["2025-01-14", "2025-01-15"]),
            "close": [10.0, 11.0],
        })
        result = find_previous_close(daily, "2025-01-15")
        assert result == 10.0  # 不包括当天

    def test_no_prior_data(self) -> None:
        daily = pd.DataFrame({
            "timestamp": pd.to_datetime(["2025-01-15"]),
            "close": [11.0],
        })
        result = find_previous_close(daily, "2025-01-15")
        assert result is None

    def test_empty_df(self) -> None:
        result = find_previous_close(pd.DataFrame(), "2025-01-15")
        assert result is None


class TestEnrichLiveBar:
    def test_enrich_no_daily(self) -> None:
        live = pd.DataFrame([{
            "timestamp": pd.Timestamp("2025-01-15"),
            "close": 11.0, "volume": 1000,
        }])
        result = enrich_live_bar(live, None)
        assert len(result) == 1
