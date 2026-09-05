"""MarketDataProvider —— 统一门面。

行情数据提供者，负责：
- 生命周期管理（refresh）
- 委托给各领域服务
- 对外提供统一 API
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
from loguru import logger

from datetime import datetime, timedelta

from src.core.clock import Clock, TimeConfig
from src.core.timezone import BEIJING_TZ
from src.market.data.cache import CacheManager
from src.market.live.xt_provider import XTQuantProvider
from src.market.services.bars import BarService
from src.market.services.breadth import BreadthService
from src.market.services.limit import LimitTracker
from src.market.services.price import PriceService
from src.market.services.resolver import Resolver
from src.market.services.sector import SectorService
from src.market.services.snapshot import SnapshotService
from src.market.session_builder import build_session
from src.market.types import PriceDict, SectorOverviewDict, SnapshotDict, ZdtRecordDict



class MarketDataProvider:
    """行情数据提供者 —— 统一门面。

    用法：
        provider = MarketDataProvider(klines_path="/data/klines")
        provider.refresh()
        bars = provider.get_bars("000001.SZ")
        price = await provider.get_realtime_price("000001.SZ")
    """

    def __init__(self, klines_path: str | Path, clock: Clock | None = None) -> None:
        self._klines_path = Path(klines_path)
        self._clock = clock or Clock(TimeConfig(
            start_time=datetime.now(BEIJING_TZ),
            tick_duration=timedelta(seconds=1),
            realtime=True,
        ))
        self._cache = CacheManager()
        self._xt = XTQuantProvider(self._cache)

        # 组装服务（显式依赖注入，无服务定位器）
        self._bar_svc = BarService(self._cache, self._clock, self._klines_path)
        self._price_svc = PriceService(self._cache, self._bar_svc, self._clock)
        self._limit_tracker = LimitTracker(self._cache, self._bar_svc, self._clock)
        self._sector_svc = SectorService(self._cache, self._bar_svc, self._price_svc, self._clock, str(self._klines_path))

        self._snapshot_svc = SnapshotService(self._cache, self._bar_svc, self._clock, str(self._klines_path))
        self._breadth_svc = BreadthService(self._cache, self._bar_svc, self._clock)

        self._resolver = Resolver(self._cache)

        self.refresh()

    # ── 属性 ──

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def klines_path(self) -> Path:
        return self._klines_path

    @property
    def trading_days(self) -> list[str]:
        return self._cache.session.daily_window

    @property
    def is_trading_day(self) -> bool:
        """今天是否为交易日。"""
        today_str = self._clock.today_str
        return today_str in self._cache.session.daily_window

    @property
    def xt_ready(self) -> bool:
        return self._xt.ready

    # ── 生命周期 ──

    def refresh(self, force: bool = False) -> None:
        """刷新 Session 数据（从磁盘重新加载）。"""
        today_str = self._clock.today_str

        loaded_date = self._cache.session.adhoc.get("loaded_date")
        if not force and loaded_date == today_str:
            logger.debug("refresh skipped: already loaded {}", today_str)
            return

        logger.info("refreshing session data for {}", today_str)
        session = build_session(self._cache, self._klines_path, today_str)
        self._cache.replace_session(session)
        self._cache.clear_tick_buffer()
        self._xt.clear()
        logger.info("session refreshed for {}", today_str)

    # ── K 线查询 ──

    def get_bars(
        self,
        ticker: str,
        granularity: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame | None:
        """获取 K 线数据。"""
        return self._bar_svc.get_bars(ticker, granularity, start, end)

    def get_concept_kline(
        self,
        concept_code: str,
        granularity: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame | None:
        """获取概念 K 线。"""
        return self._bar_svc.get_bars(concept_code, granularity, start, end)

    def get_price_history(
        self, ticker: str, from_date: str | None = None, to_date: str | None = None,
    ) -> dict:
        """获取历史价格摘要（返回 dict 格式，供工具层消费）。"""
        df = self.get_bars(ticker, granularity="1d", start=from_date, end=to_date)
        if df is None or df.empty:
            return {"error": f"无历史数据: {ticker}", "ticker": ticker}
        records = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        records["timestamp"] = records["timestamp"].dt.strftime("%Y-%m-%d")
        data = records.rename(columns={"timestamp": "date"}).to_dict(orient="records")
        first_close = float(df["close"].iloc[0])
        last_close = float(df["close"].iloc[-1])
        pct = round((last_close - first_close) / first_close * 100, 2) if first_close else 0.0
        return {
            "ticker": ticker,
            "from": from_date or "",
            "to": to_date or "",
            "count": len(data),
            "data": data,
            "first_close": first_close,
            "last_close": last_close,
            "pct_chg": pct,
            "period_high": float(df["high"].max()),
            "period_low": float(df["low"].min()),
            "source": "csv",
        }

    # ── 实时价格 ──

    async def get_realtime_price(self, ticker: str) -> PriceDict:
        """获取实时价格。自动降级到历史数据。"""
        result = await self._price_svc.get_realtime_price(ticker)
        if not result.get("available"):
            # 降级到最近一次已知价格
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
                    source="daily_fallback",
                    available=True,
                )
        return result

    async def get_realtime_prices(self, tickers: list[str]) -> dict[str, PriceDict]:
        """批量获取实时价格。"""
        return await self._price_svc.get_realtime_prices(tickers)

    # ── 换手率（纯统计数据，不依赖技术指标计算）──

    def get_turnover_rate(self, ticker: str) -> float | None:
        """估算换手率 = 昨日换手率 × (今日量 / 昨日量)。

        防御：
        - 昨日量为 0 → 返回 None
        - 今日量为 0（盘前）→ 返回昨日换手率
        """
        last_dt = self._cache.get_last_daily(ticker)
        if last_dt is None or last_dt.volume <= 0:
            return None

        df_1m = self._bar_svc.get_live_1m(ticker)
        if df_1m is None or df_1m.empty:
            return float(last_dt.turnover_rate)

        today_vol = float(df_1m["volume"].sum())
        if today_vol <= 0:
            return float(last_dt.turnover_rate)

        vol_ratio = today_vol / last_dt.volume
        return last_dt.turnover_rate * vol_ratio

    # ── 涨跌停 ──

    def get_zdt_record(self, ticker: str) -> ZdtRecordDict | None:
        """获取涨跌停记录。"""
        return self._limit_tracker.get_zdt_record(ticker)

    # ── 板块分析 ──

    async def get_sector_overview(self, sector: str) -> SectorOverviewDict:
        """获取板块概览。"""
        return await asyncio.to_thread(self._sector_svc.get_sector_overview, sector)

    def get_sector_leader(self, sector_code: str) -> dict:
        """获取板块龙头。"""
        return self._sector_svc.get_sector_leader(sector_code)

    def get_sector_volume_ratio(self, sector_code: str, n: int = 5) -> dict:
        """获取板块量比。"""
        return self._sector_svc.get_sector_volume_ratio(sector_code, n)

    def get_concept_list(self, con_type: str = "all") -> list[dict]:
        """获取概念列表。"""
        return self._sector_svc.get_concept_list(con_type)

    def get_concept_members(self, concept_code: str) -> list[str]:
        """获取概念成员。"""
        return self._sector_svc.get_concept_members(concept_code)

    def get_stock_concepts(self, ticker: str) -> dict:
        """获取股票所属概念。"""
        return self._sector_svc.get_stock_concepts(ticker)


    def get_sector_intraday(self, sector_code: str, include_bars: bool = False) -> dict:
        """获取板块日内实时数据。"""
        return self._sector_svc.get_sector_intraday(sector_code, include_bars)

    # ── 快照 ──

    def get_market_snapshot(self, date: str) -> SnapshotDict:
        """获取市场快照。"""
        return self._snapshot_svc.get_market_snapshot(date)


    # ── 市场广度 ──

    def get_market_breadth(self) -> dict:
        """获取全市场涨跌统计。"""
        return self._breadth_svc.compute_market_breadth()

    def get_index_overview(self) -> dict:
        """获取主要指数概览。"""
        return self._breadth_svc.get_index_overview()

    # ── 解析 ──

    def resolve_stock_ticker(self, name: str) -> list[tuple[str, str]]:
        """按名称查找股票。"""
        return self._resolver.resolve_stock_ticker(name)

    def infer_stock_market(self, name: str) -> tuple[str, str, str] | None:
        """推断股票所属市场。Returns (market_type, ticker, resolved_name) 或 None。"""
        return self._resolver.infer_stock_market(name)

    def resolve_index_name(self, name: str) -> str | None:
        """解析指数名称。"""
        return self._resolver.resolve_index_name(name)

    def get_stock_name(self, ticker: str) -> str:
        """获取股票名称。"""
        return self._resolver.get_stock_name(ticker)

    def resolve_sector_code(self, sector: str) -> str | None:
        """解析板块代码。"""
        return self._resolver.resolve_sector_code(sector)

    # ── 数据访问 ──

    def get_classification(self) -> dict:
        """获取板块分类数据。"""
        return self._cache.session.classification

    def get_zdt_follow_through(self) -> dict:
        """计算昨日涨停股今日表现（赚钱效应指标）。"""
        return self._limit_tracker.get_zdt_follow_through()

