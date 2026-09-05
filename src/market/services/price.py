"""PriceService —— 实时定价。

在实盘模式下通过 tick 获取最新价格，模拟模式下通过 1m 数据获取。
"""

from __future__ import annotations

import asyncio
from loguru import logger

import pandas as pd

from src.core.clock import Clock
from src.market.compute.bars import calc_pct_chg
from src.market.data.cache import CacheManager
from src.market.services.bars import BarService
from src.market.types import PriceDict



class PriceService:
    """实时价格查询。"""

    def __init__(self, cache: CacheManager, bar_svc: BarService, clock: Clock) -> None:
        self._cache = cache
        self._bar_svc = bar_svc
        self._clock = clock

    async def get_realtime_price(self, ticker: str) -> PriceDict:
        """获取单只股票的实时价格。"""
        return await asyncio.to_thread(self._get_price_sync, ticker)

    async def get_realtime_prices(self, tickers: list[str]) -> dict[str, PriceDict]:
        """批量获取实时价格。

        注意：对于大量股票（>100），建议分批调用以避免线程池膨胀。
        我们使用 asyncio.gather 但限制并发数。
        """
        from src.config import settings
        sem = asyncio.Semaphore(settings.price_fetch_concurrency)

        async def _bounded(tkr: str) -> PriceDict:
            async with sem:
                try:
                    return await asyncio.to_thread(self._get_price_sync, tkr)
                except Exception:
                    logger.warning("failed to fetch price for {}", tkr, exc_info=True)
                    return PriceDict(ticker=tkr, price=0.0, available=False)

        tasks = [_bounded(t) for t in tickers]
        results = await asyncio.gather(*tasks)
        return dict(zip(tickers, results, strict=True))

    def _get_price_sync(self, ticker: str) -> PriceDict:
        """同步获取实时价格。

        实盘模式 → 最后一条 tick；模拟模式 → 1m 数据（时钟截断）。
        """
        try:
            return self._price_sync_inner(ticker)
        except Exception:
            logger.warning("unexpected error fetching price for {}", ticker, exc_info=True)
            return PriceDict(ticker=ticker, price=0.0, available=False)

    def _price_sync_inner(self, ticker: str) -> PriceDict:
        """内部实现，不捕获异常。"""
        if self._clock.is_realtime:
            ticks = self._cache.get_tick_dict(ticker)
            tick = ticks[-1] if ticks else None
            if tick:
                return self._price_from_tick(ticker, tick)
            # 无 tick 数据，用昨日收盘兜底
            last_dt = self._cache.get_last_daily(ticker)
            if last_dt:
                return PriceDict(
                    ticker=ticker,
                    price=last_dt.close,
                    open=last_dt.open,
                    high=last_dt.high,
                    low=last_dt.low,
                    close=last_dt.close,
                    pre_close=last_dt.pre_close,
                    pct_chg=0.0,
                    volume=0.0,
                    amount=0.0,
                    source="daily",
                    available=True,
                )

        # 模拟模式：按时钟截断 1m 数据
        minutes = self._clock.minutes_since_midnight
        if minutes < 9 * 60 + 30:
            # 盘前：返回昨日全天
            df = self._cache.get_last_1m(ticker)
        else:
            df = self._cache.get_today_1m(ticker)
            if df is None or df.empty:
                df = self._cache.get_last_1m(ticker)
            elif minutes < 15 * 60:
                # 盘中：截断到当前分钟
                bar_index = 120 + (minutes - 13 * 60) if minutes >= 13 * 60 else min(minutes - (9 * 60 + 30), 119)
                bar_index = max(0, min(bar_index, len(df) - 1))
                df = df.iloc[:bar_index + 1]
            # 盘后：返回全天（不截断）

        if df is None or df.empty:
            return PriceDict(ticker=ticker, price=0.0, available=False)

        latest = df.iloc[-1]
        last_dt = self._cache.get_last_daily(ticker)
        prev_close = last_dt.close if last_dt else 0.0
        latest_close = float(latest["close"])

        return PriceDict(
            ticker=ticker,
            price=latest_close,
            open=float(df["open"].iloc[0]),
            high=float(df["high"].max()),
            low=float(df["low"].min()),
            close=latest_close,
            pre_close=prev_close,
            pct_chg=round(calc_pct_chg(latest_close, prev_close) or 0.0, 2) if prev_close else None,
            volume=float(df["volume"].sum()),
            amount=float(df["amount"].sum()),
            source="1m",
            available=True,
        )

    @staticmethod
    def _price_from_tick(ticker: str, tick: dict | pd.Series) -> PriceDict:
        """从 tick 数据提取价格。"""
        last_price = tick.get("lastPrice", 0) if isinstance(tick, dict) else tick.get("lastPrice", 0)
        last_close = tick.get("lastClose", 0) if isinstance(tick, dict) else tick.get("lastClose", 0)
        pct = round(calc_pct_chg(float(last_price), float(last_close)) or 0.0, 2) if last_close else None

        return PriceDict(
            ticker=ticker,
            price=float(last_price),
            open=float(tick.get("open", 0)),
            high=float(tick.get("high", 0)),
            low=float(tick.get("low", 0)),
            close=float(last_price),
            pre_close=float(last_close),
            pct_chg=pct,
            volume=float(tick.get("volume", 0)) / 10000.0,
            amount=float(tick.get("amount", 0)) / 10000.0,
            source="xtquant",
            available=True,
        )
