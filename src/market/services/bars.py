"""BarService —— 统一 OHLCV 查询 + 时钟截断。

所有 K 线获取的唯一入口。严格遵守 问题.md 行为规范：

日线：
- 默认不传 start/end → 从 clock 往前 120 个交易日
- end 超过 clock 日期 → 截断到 clock 日期
- 0-9:30：不含当天日线
- 9:30-15:00：用实时日线替换当天
- 15:00-24:00：直接使用日线数据（含当天）
- 对 stock/index/concept 均适用

分钟：
- 忽略 start/end，只返回当前交易日数据
- 9:30 以前 → 返回上个交易日
- 9:30-15:00 → 返回实时数据（截断到当前时间）
- 15:00 之后 → 返回全天数据
- 实时分钟只读缓存，禁止从 CSV 加载
"""

from __future__ import annotations

from loguru import logger
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.core.clock import Clock
from src.market.compute.bars import build_daily_bar_from_1m, enrich_live_bar
from src.market.data.cache import CacheManager
from src.market.types import DailyTicker


# 默认日线查询窗口（日历日，非交易日）
_DEFAULT_DAILY_DAYS = 180  # 约 120 个交易日（含周末节假日余量）

# 指数代码集合（惰性加载）
_INDEX_CODES: set[str] | None = None


def _get_index_codes() -> set[str]:
    global _INDEX_CODES
    if _INDEX_CODES is None:
        from src.market.config import INDEX_CODES
        _INDEX_CODES = set(INDEX_CODES)
    return _INDEX_CODES


def _infer_category(ticker: str) -> str:
    """从 ticker 推导类型：
    - .TI 后缀 → concept
    - 在 INDEX_CODES 中 → index
    - 其余 → stock
    """
    if ticker.endswith(".TI"):
        return "concept"
    if ticker in _get_index_codes():
        return "index"
    return "stock"


def _daily_ticker_to_df(dt: DailyTicker) -> pd.DataFrame:
    """将 DailyTicker 转为单行 DataFrame，用于拼接到历史日线后面。"""
    row = {
        "timestamp": pd.Timestamp(dt.timestamp, unit="ms"),
        "open": dt.open, "high": dt.high, "low": dt.low, "close": dt.close,
        "volume": dt.volume, "amount": dt.amount,
        "pre_close": dt.pre_close,
        "turnover_rate": dt.turnover_rate,
        "circ_mv": dt.circ_mv,
        "total_mv": dt.total_mv,
    }
    if dt.pe:
        row["pe"] = dt.pe
    if dt.pb:
        row["pb"] = dt.pb
    if dt.float_share:
        row["float_share"] = dt.float_share
    return pd.DataFrame([row])


def _build_today_bar_from_1m(df_1m: pd.DataFrame, date_str: str, prev_close: float) -> pd.DataFrame:
    """从 1m 数据合成一根日线 Bar。"""
    return pd.DataFrame([{
        "timestamp": pd.Timestamp(date_str),
        "open": float(df_1m["open"].iloc[0]),
        "high": float(df_1m["high"].max()),
        "low": float(df_1m["low"].min()),
        "close": float(df_1m["close"].iloc[-1]),
        "volume": float(df_1m["volume"].sum()),
        "amount": float(df_1m["amount"].sum()),
        "pre_close": prev_close,
    }])


class BarService:
    """统一 OHLCV 查询。"""

    def __init__(self, cache: CacheManager, clock: Clock, klines_path: str | Path) -> None:
        self._cache = cache
        self._clock = clock
        self._klines_path = Path(klines_path)

    # ── 主查询 ──

    def get_bars(
        self,
        ticker: str,
        granularity: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame | None:
        """获取 K 线数据。

        参数：
            ticker: 股票/指数/概念代码
            granularity: 粒度（1m/1d/5m/15m/30m/60m/1w/1M）
            start/end: 日期范围（可选，YYYYMMDD 或 YYYY-MM-DD）

        返回：归一化的 OHLCV DataFrame 或 None。
        """
        is_intraday = granularity in ("1m", "5m", "15m", "30m", "60m")

        # 规范化 end/start 格式（去掉连字符，统一 YYYYMMDD）
        end = end.replace("-", "") if end else None
        start = start.replace("-", "") if start else None

        # 从 ticker 推导 category
        category = _infer_category(ticker)

        # 规范化 end/start 范围（截断到 clock 日期）
        if not end or end > self._clock.today_str:
            end = self._clock.today_str
        if not start or start > end:
            start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=_DEFAULT_DAILY_DAYS)).strftime("%Y%m%d")

        # ── 实盘模式 ──
        if self._clock.is_realtime:
            if is_intraday:
                df = self.get_live_1m(ticker)
                if df is None or df.empty:
                    return None
            else:
                df = self._load_daily(ticker, start, end, category)
                if df is None or df.empty:
                    return None
                # 实盘：today_daily_ticker 由 xtquant 实时推送，直接拼到末尾
                if end == self._clock.today_str:
                    today = self._cache.get_today_daily(ticker)
                    if today is not None:
                        today_df = _daily_ticker_to_df(today)
                        df = pd.concat([df, today_df], ignore_index=True)

        # ── 模拟模式 ──
        else:
            if is_intraday:
                df = self.get_live_1m(ticker)
                if df is None or df.empty:
                    return None
            else:
                df = self._load_daily(ticker, start, end, category)
                if df is None or df.empty:
                    return None
                # 模拟：从 CSV 加载的日线含当天数据，按时钟阶段处理
                if end == self._clock.today_str:
                    last_ts = df.iloc[-1]["timestamp"]
                    if isinstance(last_ts, pd.Timestamp):
                        last_date = last_ts.strftime("%Y%m%d")
                    elif pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        last_date = pd.Timestamp(last_ts).strftime("%Y%m%d")
                    else:
                        last_date = str(last_ts)[:8]

                    if self._clock.is_pre_market and last_date == end:
                        # 盘前：去掉 CSV 今日数据，不重建（9:30 前不含当天日线）
                        df = df.iloc[:-1]
                    elif self._clock.is_trading_session:
                        # 盘中：去掉 CSV 今日数据，用 1m 截断数据重建
                        if last_date == end:
                            df = df.iloc[:-1]
                        df_1m = self.get_live_1m(ticker)
                        if df_1m is not None and not df_1m.empty:
                            prev_close = float(df.iloc[-1]["close"]) if not df.empty else 0.0
                            today_bar = _build_today_bar_from_1m(df_1m, end, prev_close)
                            today_bar = enrich_live_bar(today_bar, self._cache.get_last_daily(ticker))
                            if category == "index":
                                # 指数需要补充 index_code/index_name/change/pct_chg
                                from src.market.config import INDEX_CODE_TO_NAME

                                idx_today_close = float(today_bar["close"].iloc[0])
                                today_bar.loc[today_bar.index[0], "index_code"] = ticker
                                today_bar.loc[today_bar.index[0], "ts_code"] = ticker
                                today_bar.loc[today_bar.index[0], "index_name"] = INDEX_CODE_TO_NAME.get(ticker, "")
                                if prev_close > 0:
                                    today_bar.loc[today_bar.index[0], "change"] = idx_today_close - prev_close
                                    today_bar.loc[today_bar.index[0], "pct_chg"] = round(
                                        (idx_today_close - prev_close) / prev_close * 100, 4)
                            df = pd.concat([df, today_bar], ignore_index=True)
                    # 盘后：保留 CSV 今日数据（市场已收盘，数据为最终值）

        # ── 重采样（非基础粒度）──
        if granularity not in ("1m", "1d"):
            from src.market.compute.resample import resample_bars
            df = resample_bars(df, granularity)

        # ── 确保 pre_close ──
        if df is not None and not df.empty:
            df = df.sort_values("timestamp").reset_index(drop=True)
            if "pre_close" not in df.columns:
                df["pre_close"] = df["close"].shift(1)
            return df
        return None

    # ── 日线加载 ──

    def _load_daily(
        self, ticker: str, start: str, end: str, category: str = "stock",
    ) -> pd.DataFrame | None:
        """Load daily bars covering [start, end], with cache-first lookup."""

        # ── 1. Check cache ──
        df = self._cache.session.daily_history.get(ticker)
        if df is not None:
            return self._filter_by_date(df, ticker, start, end)

        # ── 2. Load from CSV ──
        from src.market.data.loader import (
            load_concept_kline_one,
            load_index_daily,
            load_stock_daily,
        )

        if category == "stock":
            df = load_stock_daily(ticker, self._klines_path, days=0)
        elif category == "index":
            all_idx = load_index_daily(self._klines_path, days=0)
            if all_idx is not None and not all_idx.empty and "index_code" in all_idx.columns:
                df = all_idx[all_idx["index_code"] == ticker].copy()
            else:
                df = None
        elif category == "concept":
            clean_code = ticker.replace(".TI", "")
            df = load_concept_kline_one(f"{clean_code}.TI", self._klines_path, days=0)
            if df is None or df.empty:
                df = load_concept_kline_one(clean_code, self._klines_path, days=0)
        else:
            return None

        if df is None or df.empty:
            logger.debug("no daily CSV data for {} ({})", ticker, category)
            return None

        # ── 3. Write to cache ──
        self._cache.session.daily_history[ticker] = df.copy()

        # ── 4. Filter by start/end ──
        return self._filter_by_date(df, ticker, start, end)

    @staticmethod
    def _filter_by_date(
        df: pd.DataFrame, ticker: str, start: str, end: str,
    ) -> pd.DataFrame | None:
        """Filter daily DataFrame by time range."""
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        df = df[(ts >= start_ts) & (ts <= end_ts)]
        if df.empty:
            logger.debug("no daily data in range [{}, {}] for {}", start, end, ticker)
        return df if not df.empty else None

    # ── 实时 1m ──

    def get_live_1m(self, ticker: str) -> pd.DataFrame | None:
        """获取当前有效的 1m 数据（仅从缓存读取，不回退到 CSV）。

        实盘：优先 today_1m，回退到 last_1m
        模拟：
        - 盘前（0-9:30）→ 返回 last_1m（昨日全天）
        - 盘中（9:30-15:00）→ 返回 today_1m，截断到当前分钟
        - 盘后（15:00+）→ 返回 today_1m（全天）
        """
        if self._clock.is_realtime:
            df = self._cache.get_today_1m(ticker)
            if df is not None and not df.empty:
                return df
            return self._cache.get_last_1m(ticker)

        # 模拟模式：按时间截断
        minutes = self._clock.minutes_since_midnight
        if minutes < 9 * 60 + 30:
            # 盘前：返回昨日
            return self._cache.get_last_1m(ticker)

        df = self._cache.get_today_1m(ticker)
        if df is None or df.empty:
            return self._cache.get_last_1m(ticker)

        if minutes < 15 * 60:
            # 盘中：截断到当前分钟（从 9:31 开始计数）
            bar_index = 120 + (minutes - 13 * 60) if minutes >= 13 * 60 else min(minutes - (9 * 60 + 30), 119)
            bar_index = max(0, min(bar_index, len(df) - 1))
            return df.iloc[:bar_index + 1]

        # 盘后：返回全天
        return df

    def get_live_daily_bar(self, ticker: str) -> pd.DataFrame | None:
        """从实时 1m 合成当日日线 Bar，用昨日日线富化。"""
        df_1m = self.get_live_1m(ticker)
        if df_1m is None or df_1m.empty:
            return None

        effective_today = self._effective_trading_day() or self._clock.today_str
        if effective_today is None:
            return None

        live = build_daily_bar_from_1m(df_1m, effective_today)
        if live is None or live.empty:
            return None

        return enrich_live_bar(live, self._cache.get_last_daily(ticker))

    # ── 内部 ──

    def _effective_trading_day(self) -> str | None:
        """返回"当前交易日"。

        盘前（0-9:30）→ 上一个交易日
        其他时段 → clock.today
        """
        window = self._cache.session.daily_window
        if self._clock.is_pre_market:
            today = self._clock.today_str
            if window and today:
                prev_days = [d for d in window if d < today]
                return prev_days[-1] if prev_days else today
        today_str = self._clock.today_str
        if today_str and today_str not in window:
            prev_days = [d for d in window if d < today_str]
            return prev_days[-1] if prev_days else today_str
        return today_str
