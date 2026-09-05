"""Bar 合成工具 —— 从 1m 数据合成实时日线、查找前收盘、富化实时 Bar。

纯函数，无副作用。
"""

from __future__ import annotations

from loguru import logger
from typing import TYPE_CHECKING

import pandas as pd

from src.market.data.normalizer import coerce_timestamp


if TYPE_CHECKING:
    from src.market.types import DailyTicker


def build_daily_bar_from_1m(df_1m: pd.DataFrame, today_key: str) -> pd.DataFrame | None:
    """从 1m 数据合成一根日线 Bar。

    参数：
        df_1m: 1m 数据，含 timestamp/open/high/low/close/volume/amount 列
        today_key: 今日日期 YYYYMMDD

    返回：单行 DataFrame 或 None。
    """
    if df_1m is None or df_1m.empty:
        logger.debug("build_daily_bar_from_1m: empty 1m data for {}", today_key)
        return None

    ts_col = coerce_timestamp(df_1m["timestamp"])
    mask = ts_col.dt.strftime("%Y%m%d") == today_key
    today_bars = df_1m[mask]

    if today_bars.empty:
        logger.debug("build_daily_bar_from_1m: no bars for {} in {}", today_key, len(df_1m))
        return None

    row: dict = {
        "timestamp": pd.Timestamp(today_key),
        "open": today_bars["open"].iloc[0],
        "high": today_bars["high"].max(),
        "low": today_bars["low"].min(),
        "close": today_bars["close"].iloc[-1],
    }
    if "volume" in today_bars.columns:
        row["volume"] = today_bars["volume"].sum()
    if "amount" in today_bars.columns:
        row["amount"] = today_bars["amount"].sum()
    return pd.DataFrame([row])


def calc_pct_chg(close: float, prev_close: float) -> float | None:
    """计算涨跌幅：(close - prev_close) / prev_close * 100。

    处理除零情况：如果 prev_close <= 0，返回 None。
    """
    if prev_close is None or prev_close <= 0:
        return None
    if close is None:
        return None
    return (close - prev_close) / prev_close * 100


def find_previous_close(daily: pd.DataFrame | None, date_key: str) -> float | None:
    """在日线数据中查找 date_key 之前最近一日的收盘价。

    参数：
        daily: 历史日线 DataFrame
        date_key: 目标日期 YYYYMMDD

    返回：前收盘价或 None。
    """
    if daily is None or daily.empty:
        logger.debug("find_previous_close: empty daily data")
        return None

    ts = coerce_timestamp(daily["timestamp"])
    target = pd.Timestamp(date_key)
    prev_rows = daily[ts < target]
    if prev_rows.empty:
        return None

    return float(prev_rows.iloc[-1]["close"])


def enrich_live_bar(live: pd.DataFrame, last_dt: DailyTicker | None = None) -> pd.DataFrame:
    """用昨日 DailyTicker 富化实时日线 Bar。

    市值类按价格比例缩放，pre_close/换手率等直接复制。

    参数：
        live: 合成的实时日线（单行 DataFrame）
        last_dt: 昨日日线 DailyTicker
    """
    if last_dt is None:
        return live

    live_close = live["close"].iloc[0]
    yesterday_close = last_dt.close

    # ts_code
    live.loc[live.index[0], "ts_code"] = last_dt.ts_code

    # pre_close
    if live.iloc[0].get("pre_close") is None or pd.isna(live.iloc[0]["pre_close"]):
        live.loc[live.index[0], "pre_close"] = yesterday_close

    # 市值类按价格比例缩放
    if yesterday_close > 0 and live_close > 0:
        ratio = live_close / yesterday_close
        live.loc[live.index[0], "circ_mv"] = last_dt.circ_mv * ratio
        live.loc[live.index[0], "total_mv"] = last_dt.total_mv * ratio

    # 静态列
    live.loc[live.index[0], "float_share"] = last_dt.float_share
    if last_dt.pe:
        live.loc[live.index[0], "pe"] = last_dt.pe
    if last_dt.pb:
        live.loc[live.index[0], "pb"] = last_dt.pb

    # 换手率（%）= 成交量 / 流通股本 × 100（与 pro.daily_basic 保持一致）
    volume = live.iloc[0].get("volume", 0)
    if last_dt.float_share > 0 and volume:
        live.loc[live.index[0], "turnover_rate"] = volume / last_dt.float_share * 100.0

    return live
