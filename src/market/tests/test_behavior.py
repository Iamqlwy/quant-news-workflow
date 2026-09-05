"""行为规范测试 —— 验证 问题.md 中定义的行为。"""

from datetime import datetime, timedelta

from src.core.clock import Clock, TimeConfig
from src.market.data.cache import CacheManager


class TestEffectiveTradingDay:
    """get_live_1m / get_bars 的交易日回退逻辑。"""

    def test_pre_market_falls_back_to_yesterday(self) -> None:
        """盘前（0-9:30），effective_trading_day 应返回上一交易日。"""
        from src.market.services.bars import BarService

        cache = CacheManager()
        cache.session.daily_window = ["20250113", "20250114", "20250115"]
        clock = Clock(TimeConfig(
            start_time=datetime(2025, 1, 16, 8, 0, 0),
            tick_duration=timedelta(minutes=1),
            realtime=False,
        ))
        svc = BarService(cache, clock, "/tmp")

        assert clock.is_pre_market is True
        assert svc._effective_trading_day() == "20250115"

    def test_during_trading_no_fallback(self) -> None:
        """盘中（9:30-15:00），effective_trading_day 应返回当天。"""
        from src.market.services.bars import BarService

        cache = CacheManager()
        cache.session.daily_window = ["20250113", "20250114", "20250115"]
        clock = Clock(TimeConfig(
            start_time=datetime(2025, 1, 15, 10, 30, 0),
            tick_duration=timedelta(minutes=1),
            realtime=False,
        ))
        svc = BarService(cache, clock, "/tmp")

        assert clock.is_trading_session is True
        assert svc._effective_trading_day() == "20250115"

    def test_post_market_no_fallback(self) -> None:
        """盘后（15:00+），effective_trading_day 应返回当天。"""
        from src.market.services.bars import BarService

        cache = CacheManager()
        cache.session.daily_window = ["20250113", "20250114", "20250115"]
        clock = Clock(TimeConfig(
            start_time=datetime(2025, 1, 15, 16, 0, 0),
            tick_duration=timedelta(minutes=1),
            realtime=False,
        ))
        svc = BarService(cache, clock, "/tmp")

        assert clock.is_post_market is True
        assert svc._effective_trading_day() == "20250115"
