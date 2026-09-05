"""市场宽度、指数概览 —— 统一从 1m bar 计算。"""

from __future__ import annotations

from loguru import logger

from src.core.clock import Clock
from src.market.compute.bars import calc_pct_chg
from src.market.config import INDEX_CODE_TO_NAME, INDEX_CODES
from src.market.data.cache import CacheManager
from src.market.services.bars import BarService


# 上证指数 + 深证成指 代码（用于计算全市场成交额）
_INDEX_MARKET_AMOUNT = ("000001.SH", "399001.SZ")


class BreadthService:
    """全市场统计。"""

    def __init__(self, cache: CacheManager, bar_svc: BarService, clock: Clock) -> None:
        self._cache = cache
        self._bar_svc = bar_svc
        self._clock = clock
        self._warned_no_last_daily = False
        self._warned_no_valid_pct = False

    # ── 市场宽度 ──

    def compute_market_breadth(self) -> dict:
        """从 1m / DailyTicker 实时计算市场宽度。"""
        session = self._cache.session
        last_daily = session.last_daily_ticker
        today_1m = session.today_1m_ticker
        last_1m = session.last_1m_ticker

        if not last_daily:
            if not self._warned_no_last_daily:
                logger.warning("no last_daily data for market breadth")
                self._warned_no_last_daily = True
            else:
                logger.debug("no last_daily data for market breadth")
            return {"error": "无数据"}
        self._warned_no_last_daily = False

        minutes = self._clock.minutes_since_midnight
        if self._clock.is_trading_session:
            bar_index = 120 + (minutes - 13 * 60) if minutes >= 13 * 60 else min(minutes - (9 * 60 + 30), 119)
        else:
            bar_index = 240  # 非交易时段取全天

        pct_chgs: list[float] = []

        for ticker, last_dt in last_daily.items():
            df_1m = today_1m.get(ticker)
            if df_1m is None or df_1m.empty:
                df_1m = last_1m.get(ticker)
                if df_1m is None or df_1m.empty:
                    continue

            idx = max(0, min(bar_index, len(df_1m) - 1))
            close = float(df_1m["close"].iat[idx])
            pct = calc_pct_chg(close, last_dt.close)
            if pct is not None:
                pct_chgs.append(pct)

        if not pct_chgs:
            if not self._warned_no_valid_pct:
                logger.warning("no valid pct_chg data for market breadth")
                self._warned_no_valid_pct = True
            else:
                logger.debug("no valid pct_chg data for market breadth")
            return {"error": "无有效数据"}
        self._warned_no_valid_pct = False

        up = sum(1 for p in pct_chgs if p > 0)
        down = sum(1 for p in pct_chgs if p < 0)
        total = len(pct_chgs)

        # 成交额：上证+深证两个指数的 1m bar amount 累加（get_bars 返回万元）
        total_amount_yi = 0.0
        for idx_code in _INDEX_MARKET_AMOUNT:
            df = self._bar_svc.get_bars(idx_code, granularity="1m")
            if df is None or df.empty:
                continue
            idx = max(0, min(bar_index, len(df) - 1))
            total_amount_yi += float(df["amount"].iloc[:idx + 1].sum())
        total_amount_yi = round(total_amount_yi / 1e4, 1)  # 万元 → 亿元

        return {
            "total": total,
            "up_count": up,
            "down_count": down,
            "up_down_ratio": round(up / max(down, 1), 2),
            "avg_pct_chg": round(sum(pct_chgs) / total, 2),
            "total_amount_yi": total_amount_yi,
            "source": "intraday_1m",
        }

    # ── 指数概览 ──

    def get_index_overview(self) -> dict:
        """主要指数涨跌幅。"""
        result: dict[str, dict] = {}
        for code in INDEX_CODES:
            df = self._bar_svc.get_bars(code, granularity="1d")
            if df is None or df.empty:
                continue
            name = INDEX_CODE_TO_NAME.get(code, code)
            latest = df.iloc[-1]
            close = float(latest["close"])
            pct_chg: float | None = None
            if len(df) >= 2:
                prev_close = float(df.iloc[-2]["close"])
                if prev_close:
                    pct_chg = round((close - prev_close) / prev_close * 100, 2)
            result[name] = {
                "code": code,
                "name": name,
                "close": round(close, 2),
                "pct_chg": pct_chg,
                "volume": float(latest["volume"]),
                "amount": float(latest["amount"]),
            }
        return result

