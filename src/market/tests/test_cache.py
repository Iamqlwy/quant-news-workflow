"""缓存管理器测试。"""


from src.market.data.cache import CacheManager
from src.market.types import DailyTicker, SessionData


class TestCacheManager:
    def test_session_initial_empty(self) -> None:
        mgr = CacheManager()
        assert mgr.session.daily_window == []
        assert mgr.session.last_daily_ticker == {}

    def test_replace_session(self) -> None:
        mgr = CacheManager()
        new = SessionData(daily_window=["20250101", "20250102"])
        mgr.replace_session(new)
        assert mgr.session.daily_window == ["20250101", "20250102"]

    def test_get_last_daily(self) -> None:
        mgr = CacheManager()
        dt = DailyTicker(
            ts_code="000001.SZ", open=10.0, close=10.5, high=11.0, low=9.5,
            pre_close=10.0, volume=1000.0, amount=10500.0, volume_ratio=1.0,
            turnover_rate=1.0, turnover_rate_f=1.0, pe=10.0, pe_ttm=10.0,
            pb=1.0, ps=1.0, ps_ttm=1.0, dv_ratio=0.0, dv_ttm=0.0,
            total_share=10000.0, float_share=8000.0, free_share=5000.0,
            total_mv=105000.0, circ_mv=84000.0, timestamp=1700000000000,
        )
        new = SessionData(last_daily_ticker={"000001.SZ": dt})
        mgr.replace_session(new)
        result = mgr.get_last_daily("000001.SZ")
        assert result is not None
        assert result.close == 10.5

    def test_get_last_daily_missing(self) -> None:
        mgr = CacheManager()
        assert mgr.get_last_daily("NONEXIST") is None

    def test_session_snapshot_returns_copy(self) -> None:
        mgr = CacheManager()
        mgr.session.daily_window = ["20250101"]
        snap = mgr.session_snapshot()
        snap.daily_window.append("20250102")
        assert mgr.session.daily_window == ["20250101"]

    def test_tick_buffer(self) -> None:
        mgr = CacheManager()
        mgr.append_tick("000001.SZ", {"price": 10.0})
        mgr.append_tick("000001.SZ", {"price": 10.5})
        df = mgr.get_tick_df("000001.SZ")
        assert df is not None
        assert len(df) == 2

    def test_tick_buffer_clear(self) -> None:
        mgr = CacheManager()
        mgr.append_tick("000001.SZ", {"price": 10.0})
        mgr.clear_tick_buffer()
        assert mgr.get_tick_df("000001.SZ") is None
