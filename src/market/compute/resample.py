"""Bar 频率重采样 —— 从 1m 数据降采样到更大的时间粒度。

纯函数，无副作用。
"""

from __future__ import annotations

import pandas as pd

from src.market.config import _RESAMPLE_FREQ


def resample_bars(df: pd.DataFrame, target_granularity: str) -> pd.DataFrame:
    """将 OHLCV Bar DataFrame 重采样到目标粒度。

    参数：
        df: 源 DataFrame，需含 timestamp/open/high/low/close/volume/amount
        target_granularity: 目标粒度（5m/15m/30m/60m/1w/1M）

    返回：重采样后的 DataFrame。
    """
    freq = _RESAMPLE_FREQ.get(target_granularity)
    if freq is None or df.empty:
        return df

    if "timestamp" not in df.columns:
        return df

    df = df.copy()
    df = df.set_index("timestamp")

    agg_rules = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    }
    existing = {col: agg_rules[col] for col in agg_rules if col in df.columns}
    if not existing:
        df = df.reset_index()
        return df

    resampled = df.resample(freq).agg(existing)
    # 剔除无交易的周期（节假日产生的空窗口）
    drop_cols = ["close"] if "close" in resampled.columns else None
    return resampled.dropna(subset=drop_cols).reset_index() if drop_cols else resampled.dropna().reset_index()
