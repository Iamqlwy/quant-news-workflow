"""列归一化 —— 列名重命名 + 数值类型转换 + 单位转换。

所有从 CSV 加载的数据在进入缓存之前都经过此模块处理。
归一化后的数据：
- 时间列：datetime64
- 成交额：万元
- 成交量：万股
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from src.market.config import (
    DAILY_AMOUNT_DIVISOR,
    DAILY_VOLUME_DIVISOR,
    INTRADAY_AMOUNT_DIVISOR,
    INTRADAY_VOLUME_DIVISOR,
    OHLCV_COLUMNS,
    STOCK_1M_RENAME,
)




def coerce_timestamp(series: pd.Series) -> pd.Series:
    """智能推断时间戳格式并转换为 datetime64。

    支持 datetime64、Unix 数值（纳秒/毫秒/秒）、字符串。
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().any():  # type: ignore[union-attr]
        max_value = float(numeric.max())  # type: ignore[arg-type, union-attr]
        if max_value > 1e14:
            return pd.to_datetime(numeric, unit="ns", errors="coerce")
        if max_value > 1e11:
            return pd.to_datetime(numeric, unit="ms", errors="coerce")
        if max_value > 1e9:
            return pd.to_datetime(numeric, unit="s", errors="coerce")
    result = pd.to_datetime(series, errors="coerce")
    if result.isna().all():  # type: ignore[union-attr]
        logger.warning("coerce_timestamp: all values failed to parse")
    return result


def normalize_date_column(series: pd.Series) -> pd.Series:
    """将各种日期格式统一为 YYYYMMDD 字符串。

    处理 datetime64、YYYY-MM-DD、YYYY/MM/DD、YYYYMMDD 等格式。
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.strftime("%Y%m%d")
    return series.astype(str).str.replace("-", "", regex=False).str.replace("/", "", regex=False).str[:8]


def compact_to_ymd(date_str: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    if isinstance(date_str, str) and len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str


def normalize_1m_columns(df: pd.DataFrame, keep_extra: list[str] | None = None) -> pd.DataFrame:
    """1m 数据列归一化：中文→英文重命名 + 数值转换 + 时间排序 + 列选择。

    单位转换由调用方在拆分 per-ticker 字典时执行。
    """
    df = df.rename(columns=STOCK_1M_RENAME)
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        df["timestamp"] = coerce_timestamp(df["timestamp"])
    df = df.dropna(subset=["timestamp"])

    numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turnover_rate", "float_shares", "total_shares"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("timestamp").reset_index(drop=True)

    keep = ["timestamp", "open", "high", "low", "close", "volume", "amount"]
    if keep_extra:
        keep = keep_extra + keep
    else:
        for extra in ["turnover_rate", "float_shares", "total_shares"]:
            if extra in df.columns:
                keep.append(extra)
    existing = [c for c in keep if c in df.columns]
    return df[existing]


def apply_intraday_units(df: pd.DataFrame) -> pd.DataFrame:
    """对 1m 数据应用单位转换：amount/10000（元→万元），volume/10000（股→万股）。

    原地修改，避免大 DataFrame 的全量拷贝。
    """
    if "amount" in df.columns:
        df["amount"] = df["amount"] / INTRADAY_AMOUNT_DIVISOR
    if "volume" in df.columns:
        df["volume"] = df["volume"] / INTRADAY_VOLUME_DIVISOR
    return df


def apply_daily_units(df: pd.DataFrame) -> pd.DataFrame:
    """对日线数据应用单位转换：amount/10（元→万元），volume/100（股→万股）。

    原地修改。
    """
    if "amount" in df.columns:
        df["amount"] = df["amount"] / DAILY_AMOUNT_DIVISOR
    if "volume" in df.columns:
        df["volume"] = df["volume"] / DAILY_VOLUME_DIVISOR
    return df


def filter_df_by_date_range(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    """按 start/end 日期过滤 DataFrame（支持 YYYY-MM-DD 和 YYYYMMDD 格式）。"""
    if (not start) and (not end) or "timestamp" not in df.columns:
        return df

    ts = df["timestamp"]
    if pd.api.types.is_datetime64_any_dtype(ts):
        if start:
            df = df[ts >= pd.Timestamp(start)]
        if end:
            df = df[ts <= pd.Timestamp(end)]
        return df

    start_key = start.replace("-", "") if start else ""
    end_key = end.replace("-", "") if end else ""
    if not start_key and not end_key:
        return df

    ts_key = normalize_date_column(ts)
    if start_key:
        df = df[ts_key >= start_key]
    if end_key:
        df = df[ts_key <= end_key]
    return df


def normalize_index_1m(df: pd.DataFrame, index_code: str) -> pd.DataFrame:
    """归一化指数 1m 数据：合并日期+时间→timestamp，重命名列，数值转换。"""
    from src.market.config import INDEX_1M_RENAME

    df = df.rename(columns=INDEX_1M_RENAME)

    # 合并日期+时间列 → timestamp
    if "日期" in df.columns and "时间" in df.columns:
        time_str = df["时间"].astype(str).str.zfill(6)
        time_padded = time_str.str[:2] + ":" + time_str.str[2:4] + ":" + time_str.str[4:6]
        df["timestamp"] = pd.to_datetime(
            df["日期"].astype(str) + " " + time_padded, errors="coerce"
        )
        df = df.drop(columns=["日期", "时间"])
    elif "timestamp" not in df.columns:
        raise ValueError(f"指数 1m 数据缺少日期/时间列: {df.columns.tolist()}")

    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        df["timestamp"] = coerce_timestamp(df["timestamp"])
    df = df.dropna(subset=["timestamp"])

    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["index_code"] = index_code
    df = df.sort_values("timestamp").reset_index(drop=True)
    keep = [c for c in (OHLCV_COLUMNS + ["index_code"]) if c in df.columns]
    return df[keep]
