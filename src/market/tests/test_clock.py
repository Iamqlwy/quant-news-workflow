"""Clock 测试 —— 使用统一 Clock（原 TradingClock 测试的等价迁移）。"""

import datetime as dt
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.core.clock import Clock, TimeConfig
from src.core.timezone import BEIJING_TZ


def _sim_clock(ts_str: str) -> Clock:
    """用字符串创建模拟时钟的辅助函数。"""
    return Clock(TimeConfig(
        start_time=datetime.fromisoformat(ts_str),
        tick_duration=timedelta(minutes=1),
        realtime=False,
    ))


def _realtime_clock() -> Clock:
    """创建实盘时钟的辅助函数。"""
    return Clock(TimeConfig(
        start_time=datetime.now(BEIJING_TZ),
        tick_duration=timedelta(seconds=1),
        realtime=True,
    ))


class TestSimulationClock:
    """模拟时钟测试。"""

    def test_basic(self) -> None:
        clock = _sim_clock("2025-01-15 10:30:00")
        assert clock.today == dt.date(2025, 1, 15)
        assert clock.today_str == "20250115"
        assert clock.minutes_since_midnight == 10 * 60 + 30  # 630

    def test_is_trading_session_morning(self) -> None:
        clock = _sim_clock("2025-01-15 10:30:00")
        assert clock.is_trading_session is True
        assert clock.is_pre_market is False
        assert clock.is_post_market is False
        assert clock.phase == "trading"

    def test_is_pre_market(self) -> None:
        clock = _sim_clock("2025-01-15 08:00:00")
        assert clock.is_trading_session is False
        assert clock.is_pre_market is True
        assert clock.is_post_market is False
        assert clock.phase == "pre_market"

    def test_is_post_market(self) -> None:
        clock = _sim_clock("2025-01-15 16:00:00")
        assert clock.is_trading_session is False
        assert clock.is_pre_market is False
        assert clock.is_post_market is True
        assert clock.phase == "post_market"

    def test_boundary_930(self) -> None:
        clock = _sim_clock("2025-01-15 09:30:00")
        assert clock.is_trading_session is True

    def test_boundary_1500(self) -> None:
        clock = _sim_clock("2025-01-15 15:01:00")
        assert clock.is_trading_session is True  # 15:01 含缓冲

    def test_datetime_input(self) -> None:
        clock = Clock(TimeConfig(
            start_time=dt.datetime(2025, 6, 1, 14, 0, 0),
            tick_duration=timedelta(minutes=1),
            realtime=False,
        ))
        assert clock.today_str == "20250601"
        assert clock.phase == "trading"

    def test_invalid_time_raises(self) -> None:
        # 非法字符串在 datetime.fromisoformat 时抛出 ValueError
        with pytest.raises(ValueError):
            _sim_clock("not_a_valid_time")


class TestRealtimeClock:
    """实盘时钟测试。"""

    def test_basic(self) -> None:
        clock = _realtime_clock()
        assert clock.today is not None
        assert clock.today_str is not None
        assert len(clock.today_str) == 8

    def test_phase_is_string(self) -> None:
        clock = _realtime_clock()
        assert clock.phase in ("pre_market", "trading", "post_market")
