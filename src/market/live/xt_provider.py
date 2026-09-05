"""xtquant 连接管理 —— 全推行情订阅、Tick 缓存和 1m 聚合。"""

from __future__ import annotations

from loguru import logger
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from src.market.compute.bars import calc_pct_chg
from src.market.compute.tick_agg import TickAggregator


if TYPE_CHECKING:
    from src.market.data.cache import CacheManager


class XTQuantProvider:
    """管理 xtquant 连接、全推行情订阅、tick 缓存和 1m 聚合器。"""

    def __init__(self, cache: CacheManager) -> None:
        self._cache = cache
        self._tick_cache: dict[str, dict] = {}
        self._tick_lock = threading.Lock()
        self._ready: bool = False
        self._stock_list: list[str] = []
        self._has_xtquant: bool = False
        self._aggregator = TickAggregator(cache)
        self._connect()

    # ── 公开属性 ──

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def has_xtquant(self) -> bool:
        return self._has_xtquant

    @property
    def stock_list(self) -> list[str]:
        return self._stock_list

    @property
    def aggregator(self) -> TickAggregator:
        return self._aggregator

    def clear(self) -> None:
        """清空聚合器 buffer（refresh 时调用）。"""
        self._aggregator.clear()

    def disconnect(self) -> None:
        """断开 xtquant 连接并清理订阅。"""
        if self._has_xtquant and self._ready:
            try:
                from xtquant import xtdata
                xtdata.unsubscribe_quote(["SH", "SZ", "BJ"])
                logger.info("xtquant 订阅已清理")
            except Exception:
                logger.warning("清理 xtquant 订阅失败", exc_info=True)
            finally:
                self._ready = False

    def __del__(self) -> None:
        """析构时清理资源。"""
        self.disconnect()

    @property
    def tick_cache_snapshot(self) -> dict[str, dict]:
        """返回 tick 缓存的快照拷贝（线程安全）。"""
        with self._tick_lock:
            return dict(self._tick_cache)

    # ── tick 读写 ──

    def tick(self, ticker: str) -> dict | None:
        with self._tick_lock:
            return self._tick_cache.get(ticker)

    # ── 连接 & 回调 ──

    def _connect(self) -> None:
        """启动 xtquant 双路订阅：股票全推 + 指数代码。模拟模式跳过。"""
        try:
            from src.config import settings
        except ImportError:
            self._ready = False
            return

        if settings.simulation_enabled:
            self._ready = False
            return

        try:
            from xtquant import xtdata

            self._stock_list = xtdata.get_stock_list_in_sector("沪深京A股")
            xtdata.subscribe_whole_quote(["SH", "SZ", "BJ"], callback=self._on_tick)
            self._ready = True
            self._has_xtquant = True
        except ImportError:
            self._ready = False
        except Exception:
            logger.exception("xtquant connection failed")
            self._ready = False

    def _on_tick(self, datas: dict) -> None:
        """全推回调：更新快照缓存 + 喂入 1m 聚合器。忽略非交易时段数据。"""
        if not datas:
            return

        # 快速过滤非交易时段
        first_tick = next(iter(datas.values()))
        time_ms = first_tick.get("time", 0)
        if not time_ms:
            return

        from src.core.timezone import BEIJING_TZ
        tick_time = datetime.fromtimestamp(int(time_ms) / 1000, tz=BEIJING_TZ)
        trade_date = tick_time.date()

        morning_start = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 25, tzinfo=BEIJING_TZ)
        morning_end = datetime(trade_date.year, trade_date.month, trade_date.day, 11, 30, tzinfo=BEIJING_TZ)
        afternoon_start = datetime(trade_date.year, trade_date.month, trade_date.day, 13, 0, tzinfo=BEIJING_TZ)
        afternoon_end = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 30, tzinfo=BEIJING_TZ)

        in_trading = (morning_start <= tick_time <= morning_end) or (afternoon_start <= tick_time <= afternoon_end)
        if not in_trading:
            return

        with self._tick_lock:
            self._tick_cache.update(datas)
            self._aggregator.on_tick(datas)
            for code, tick in datas.items():
                self._cache.append_tick(code, tick)


def parse_xt_tick(ticker: str, raw: dict | None) -> dict:
    """解析 xtquant tick 数据为标准格式。"""
    if not raw:
        return {"error": "xtquant 无数据", "available": False, "ticker": ticker}

    last_price = raw.get("lastPrice", 0)
    last_close = raw.get("lastClose", 0)
    pct_chg = round(calc_pct_chg(float(last_price), float(last_close)) or 0.0, 2) if last_close else None

    return {
        "ticker": ticker,
        "price": last_price,
        "open": raw.get("open", 0),
        "high": raw.get("high", 0),
        "low": raw.get("low", 0),
        "pre_close": last_close,
        "pct_chg": pct_chg,
        "volume": raw.get("volume", 0),
        "amount": raw.get("amount", 0),
        "timetag": raw.get("timetag", ""),
        "source": "xtquant",
    }
