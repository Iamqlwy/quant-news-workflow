"""缓存管理器 —— Session 级缓存。

refresh() 时整替换，跨 cycle 存活。
"""

from __future__ import annotations

import threading

import pandas as pd

from src.market.types import DailyTicker, SessionData


class CacheManager:
    """统一缓存入口。

    用法：
        mgr = CacheManager()
        mgr.session.daily_window                 # → list[str]
        mgr.get_last_daily("000001.SZ")          # → dict | None
        mgr.get_today_1m("000001.SZ")            # → DataFrame | None
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.session = SessionData()
        self._tick_buffer: dict[str, list[dict]] = {}

    # ── Session 级 ──

    def replace_session(self, new: SessionData) -> None:
        with self._lock:
            self.session = new

    # 个股日线 —— DailyTicker 实例
    def get_last_daily(self, ticker: str) -> DailyTicker | None:
        return self.session.last_daily_ticker.get(ticker)

    def get_today_daily(self, ticker: str) -> DailyTicker | None:
        return self.session.today_daily_ticker.get(ticker)

    # 个股 1m —— DataFrame
    def get_last_1m(self, ticker: str) -> pd.DataFrame | None:
        return self.session.last_1m_ticker.get(ticker)

    def get_today_1m(self, ticker: str) -> pd.DataFrame | None:
        return self.session.today_1m_ticker.get(ticker)

    def session_snapshot(self) -> SessionData:
        with self._lock:
            import copy
            return copy.deepcopy(self.session)

    # ── Tick 缓冲（线程安全，refresh 时清空）──

    def append_tick(self, code: str, tick: dict) -> None:
        with self._lock:
            self._tick_buffer.setdefault(code, []).append(tick)

    def get_tick_df(self, code: str) -> pd.DataFrame | None:
        with self._lock:
            ticks = self._tick_buffer.get(code)
            return pd.DataFrame(ticks) if ticks else None

    def get_tick_dict(self, code: str) -> list[dict] | None:
        with self._lock:
            return self._tick_buffer.get(code)

    def clear_tick_buffer(self) -> None:
        with self._lock:
            self._tick_buffer.clear()
