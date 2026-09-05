"""CSV 数据加载器 —— 从磁盘加载所有行情数据。

所有加载函数在返回前完成：
1. 列名归一化
2. 单位转换（万元/万股）
3. 时间列转换为 datetime64

加载函数分为：
- 日线：load_daily_window（合并所有股票）、load_stock_daily（单股票）
- 分钟：load_1m_bulk_df（批量，通过 indexer）
- 指数：load_index_daily、load_index_1m
- 概念：load_concept_kline、load_concept_members
- 分类：load_classification、load_stock_basic、load_stock_name_history
- 交易日：get_recent_trading_days
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from loguru import logger

from src.market.config import (
    CONCEPT_KLINE_RENAME,
    INDEX_CODES,
    INDEX_DAILY_FILE,
    INDEX_NAME_TO_CODE,
    OHLCV_COLUMNS,
    STOCK_DAILY_RENAME,
)
from src.market.data.indexer import load_1m_bulk_df
from src.market.data.normalizer import apply_daily_units, apply_intraday_units



def read_csv_safe(path: Path, **kwargs) -> pd.DataFrame | None:
    """安全读取 CSV，文件缺失/空文件/异常时返回 None。"""
    if not path.exists():
        return None

    # 尝试常见编码
    encodings = ['utf-8', 'gbk', 'gb2312', 'latin1']
    encoding = kwargs.pop('encoding', None)
    if encoding:
        encodings.insert(0, encoding)

    for enc in encodings:
        try:
            df = pd.read_csv(path, encoding=enc, **kwargs)
            return df if not df.empty else None
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            logger.warning("CSV read failed ({}): {}, error: {}", enc, path, exc)
            return None

    logger.error("all encodings failed: {}", path)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 日线
# ═══════════════════════════════════════════════════════════════════════════════


def load_daily_window(klines_root: Path, dates: list[str]) -> pd.DataFrame:
    """加载指定日期的全市场合并日线数据。

    从 extra/all_stocks_daily/{date}.csv 读取，合并 extra/all_daily_basic/{date}.csv 的基本面数据。
    返回归一化后的 DataFrame（单位已转换）。
    """
    frames: list[pd.DataFrame] = []
    for date_str in dates:
        daily_path = klines_root / "extra" / "all_stocks_daily" / f"{date_str}.csv"
        df = read_csv_safe(daily_path)
        if df is None:
            continue

        df = df.rename(columns=STOCK_DAILY_RENAME)
        for col in OHLCV_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 合并基本面数据
        basic_path = klines_root / "extra" / "all_daily_basic" / f"{date_str}.csv"
        basic_df = read_csv_safe(basic_path)
        if basic_df is not None and "ts_code" in basic_df.columns:
            extra_cols = ["ts_code", "turnover_rate", "pe", "pb", "float_share", "total_mv", "circ_mv"]
            existing = [c for c in extra_cols if c in basic_df.columns]
            df = df.merge(basic_df[existing], on="ts_code", how="left")

        df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y%m%d", errors="coerce")
        if "timestamp" in df.columns:
            df = df.dropna(subset=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    consolidated = pd.concat(frames, ignore_index=True)
    return apply_daily_units(consolidated)


def load_stock_daily(
    ticker: str,
    klines_root: Path,
    days: int = 250,
) -> pd.DataFrame | None:
    """加载单只股票的日线数据。

    从 daily/{ticker}.csv 读取，可选合并 indicator/{ticker}.csv。
    返回归一化后的 DataFrame（单位已转换）。
    """
    csv_path = klines_root / "daily" / f"{ticker}.csv"
    df = read_csv_safe(csv_path)
    if df is None:
        return None

    df = df.rename(columns=STOCK_DAILY_RENAME)

    # 读取并合并指标文件（indicator 中的列在合并前不变名）
    indicator_path = klines_root / "indicator" / f"{ticker}.csv"
    ind_df = read_csv_safe(indicator_path)
    if ind_df is not None:
        # indicator 文件也需重命名 trade_date→timestamp 以对齐
        ind_df = ind_df.rename(columns=STOCK_DAILY_RENAME)
        ind_cols = ["timestamp", "circ_mv", "turnover_rate", "pe", "pb", "float_share", "total_mv"]
        existing_ind = [c for c in ind_cols if c in ind_df.columns]
        if existing_ind:
            df = df.merge(ind_df[existing_ind], on="timestamp", how="left")

    # 数值列转换
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # 列选择：OHLCV + pre_close + 额外列
    extra_cols = ["pre_close", "ts_code", "circ_mv", "turnover_rate", "pe", "pb", "float_share", "total_mv"]
    keep = ["timestamp"] + [c for c in (OHLCV_COLUMNS[1:] + extra_cols) if c in df.columns]
    df = df[[c for c in keep if c in df.columns]]

    df = apply_daily_units(df)
    if days and len(df) > days:
        df = df.iloc[-days:]
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# 分钟
# ═══════════════════════════════════════════════════════════════════════════════


def load_stock_1m(
    ticker: str,
    klines_root: Path,
    start_date: str | None = None,
    n_days: int = 2,
) -> pd.DataFrame | None:
    """加载单只股票的 1m 数据。

    返回归一化后的 DataFrame（单位已转换）。
    """
    df = load_1m_bulk_df([ticker], klines_root, start_date or "", n_days)
    if df is None or df.empty:
        return None
    return apply_intraday_units(df)


def load_1m_map(
    tickers: list[str],
    klines_root: Path,
    start_date: str,
    n_days: int = 2,
    workers: int = 32,
) -> dict[str, pd.DataFrame] | None:
    """批量加载 1m 数据，返回 {ticker: DataFrame} 映射。

    单位已转换。
    """
    df = load_1m_bulk_df(tickers, klines_root, start_date, n_days, workers)
    if df is None or df.empty:
        return None

    df = apply_intraday_units(df)
    result: dict[str, pd.DataFrame] = {}
    for ticker, group_df in df.groupby("ticker"):
        group_df = group_df.drop(columns=["ticker"]).sort_values("timestamp").reset_index(drop=True)
        result[str(ticker)] = group_df
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 指数
# ═══════════════════════════════════════════════════════════════════════════════


def load_index_daily(klines_root: Path, days: int = 5) -> pd.DataFrame:
    """加载所有指数的日线数据（最近 N 天）。

    返回 DataFrame 包含 index_code、index_name 和 OHLCV 列。
    """
    frames: list[pd.DataFrame] = []
    for name, file_name in INDEX_DAILY_FILE.items():
        path = klines_root / "index_daily" / file_name
        df = read_csv_safe(path)
        if df is None:
            continue

        df = df.rename(columns={"trade_date": "timestamp", "vol": "volume"})
        for col in OHLCV_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["timestamp"])
        df["index_code"] = INDEX_NAME_TO_CODE.get(name, "")
        df["index_name"] = name
        df = df.sort_values("timestamp")
        if days and len(df) > days:
            df = df.iloc[-days:]
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    return apply_daily_units(result)


def load_index_1m(klines_root: Path, since_date: str | None = None) -> pd.DataFrame:
    """加载所有指数的 1m 数据。

    since_date: YYYYMMDD 格式，只加载 >= 此日期的数据。None 表示加载全部。
    返回 DataFrame 包含 index_code 和 OHLCV 列。
    """
    from src.market.config import INDEX_1M_RENAME

    frames: list[pd.DataFrame] = []
    for code in INDEX_CODES:
        path = klines_root / "index_1m" / f"{code}.csv"
        df = read_csv_safe(path)
        if df is None:
            continue

        df = df.rename(columns=INDEX_1M_RENAME)
        if "日期" in df.columns and "时间" in df.columns:
            time_str = df["时间"].astype(str)
            # 时间可能是 "09:31" 或 "0931" 格式，统一为 HH:MM:SS
            if time_str.str.contains(":").any():
                time_padded = time_str + ":00"
            else:
                time_padded = time_str.str[:2] + ":" + time_str.str[2:4] + ":" + time_str.str[4:6]
            df["timestamp"] = pd.to_datetime(
                df["日期"].astype(str) + " " + time_padded, errors="coerce"
            )
            df = df.drop(columns=["日期", "时间"])

        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["index_code"] = code
        df = df.dropna(subset=["timestamp"])
        df = df.sort_values("timestamp")

        # 按日期过滤（since_date 为 YYYYMMDD，如 "20260602"）
        if since_date and "timestamp" in df.columns:
            cut = pd.Timestamp(since_date)
            df = df[df["timestamp"] >= cut]

        if df.empty:
            continue
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    return apply_intraday_units(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 概念
# ═══════════════════════════════════════════════════════════════════════════════


def load_concept_kline(klines_root: Path, days: int = 30, valid_codes: set[str] | None = None) -> pd.DataFrame:
    """加载所有概念日线数据。

    从 concepts/kline/*.TI.csv 读取。
    """
    kline_dir = klines_root / "concepts" / "kline"
    if not kline_dir.exists():
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for path in sorted(kline_dir.glob("*.csv")):
        concept_code = path.stem.replace(".TI", "")
        if valid_codes is not None and concept_code not in valid_codes:
            continue
        df = read_csv_safe(path)
        if df is None:
            continue
        frames.append(_normalize_concept_df(df, concept_code, days))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _normalize_concept_df(df: pd.DataFrame, concept_code: str, days: int) -> pd.DataFrame:
    """归一化概念 K 线 DataFrame。"""
    df = df.rename(columns=CONCEPT_KLINE_RENAME)
    for col in OHLCV_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 补充缺失的 OHLC（某些概念只有 close）
    if "close" in df.columns:
        for col in ["open", "high", "low"]:
            if col not in df.columns or df[col].isna().all():
                df[col] = df["close"]

    df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df["concept_code"] = concept_code
    df = df.sort_values("timestamp")
    if days and len(df) > days:
        df = df.iloc[-days:]
    df = df.reset_index(drop=True)
    df = apply_daily_units(df)
    # 市值列也需要转换为万元（源数据为元）
    for mv_col in ["total_mv", "float_mv"]:
        if mv_col in df.columns:
            df[mv_col] = df[mv_col] / 10000.0
    return df


def load_concept_kline_one(
    concept_code: str,
    klines_root: Path,
    days: int = 30,
) -> pd.DataFrame | None:
    """加载单个概念的日线数据。"""
    path = klines_root / "concepts" / "kline" / f"{concept_code}.csv"
    df = read_csv_safe(path)
    if df is None:
        return None
    return _normalize_concept_df(df, concept_code, days)


def load_all_concept_members(klines_root: Path, workers: int = 16, valid_codes: set[str] | None = None) -> pd.DataFrame:
    """加载所有概念成员 → [con_code, ts_code] DataFrame。"""
    member_dir = klines_root / "concepts" / "member"
    if not member_dir.exists():
        return pd.DataFrame()

    paths = sorted(member_dir.glob("*.csv"))
    if valid_codes is not None:
        paths = [p for p in paths if p.stem.replace(".TI", "") in valid_codes]
    if not paths:
        return pd.DataFrame()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(paths))) as ex:
        futures = {ex.submit(_load_one_member, p): p for p in paths}
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                frames.append(result)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_one_member(path: Path) -> pd.DataFrame | None:
    """加载单个概念成员文件，返回标准化 DataFrame 或 None。"""
    concept_code = path.stem.replace(".TI", "")
    df = read_csv_safe(path)
    if df is None:
        return None
    if "con_code" in df.columns and "ts_code" in df.columns:
        return pd.DataFrame({
            "con_code": concept_code,
            "ts_code": df["con_code"].astype(str),
        })
    if "ts_code" in df.columns:
        df["con_code"] = concept_code
        return df[["con_code", "ts_code"]]
    if "con_code" in df.columns:
        df = df.rename(columns={"con_code": "ts_code"})
        df["con_code"] = concept_code
        return df[["con_code", "ts_code"]]
    return None


def load_concept_members_one(concept_code: str, klines_root: Path) -> list[str]:
    """加载单个概念的成员股票代码列表。"""
    path = klines_root / "concepts" / "member" / f"{concept_code}.csv"
    df = read_csv_safe(path)
    if df is None:
        return []
    # 文件格式：ts_code=概念代码, con_code=股票代码
    if "con_code" in df.columns and "ts_code" in df.columns:
        return df["con_code"].astype(str).tolist()
    if "con_code" in df.columns:
        return df["con_code"].astype(str).tolist()
    if "ts_code" in df.columns:
        return df["ts_code"].astype(str).tolist()
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# 分类
# ═══════════════════════════════════════════════════════════════════════════════


def load_classification(klines_root: Path) -> dict[str, pd.DataFrame]:
    """加载板块分类数据。

    返回：{"concept": df, "industry": df, "region": df, "industry_children": df, "stock_concepts": df}
    """
    concepts_dir = klines_root / "concepts"
    result: dict[str, pd.DataFrame] = {}

    mapping = {
        "concept": "concept_filter.csv",
        "industry": "industry.csv",
        "region": "region.csv",
        "industry_children": "industry_children.csv",
        "stock_concepts": "stock_concepts.csv",
    }
    for key, filename in mapping.items():
        df = read_csv_safe(concepts_dir / filename, dtype=str)
        result[key] = df if df is not None else pd.DataFrame()

    return result


def load_stock_basic(klines_root: Path) -> pd.DataFrame | None:
    """加载股票基本信息。"""
    return read_csv_safe(klines_root / "stock_basic.csv", dtype=str)


def load_hk_basic(klines_root: Path) -> pd.DataFrame | None:
    """加载港股基本信息。"""
    return read_csv_safe(klines_root / "hk_basic.csv", dtype=str)


def load_stock_name_history(klines_root: Path) -> pd.DataFrame | None:
    """加载股票曾用名历史。"""
    # 我们不需要详细的变更，只需要历史上出现过哪些名字
    df = read_csv_safe(klines_root / "stock_name_history_full.csv", dtype=str)
    df = df.drop_duplicates(subset=["name"])
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# 交易日
# ═══════════════════════════════════════════════════════════════════════════════


def get_recent_trading_days(klines_root: Path, ref_date: str, n: int = 30) -> list[str]:
    """获取 ref_date（含）之前最近的 N 个交易日（YYYYMMDD 格式）。

    akshare 的 CSV 文件名已过滤周末和节假日。盘中 ref_date 对应的数据文件
    可能还不存在——此时如果 ref_date 是工作日则直接视为交易日。

    失败时降级为工作日算法（周一至周五，排除五一、十一假期）。
    """
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina().sort_values(by="trade_date")
        df = df.sort_values(by="trade_date").drop_duplicates(subset=["trade_date"])
        days = df["trade_date"].astype(str).tolist()
        days = [d.replace("-", "") for d in days]

        # 过滤 ref_date 之前的交易日（含 ref_date）
        days = [d for d in days if d <= ref_date]

        # 返回最近 N 个交易日
        return days[-n:] if len(days) > n else days

    except Exception as e:
        logger.warning("akshare failed to get trading days, using workday fallback: {}", e)
        return _get_workday_fallback(ref_date, n)


def load_zdt(klines_root: Path, date_str: str) -> list[dict[str, object]]:
    """加载指定日期的 ZDT 数据，返回 ZdtRecordDict 列表。

    CSV 来源：tushare pro.limit_list_ths，只保留 涨停池/连扳池 记录。
    """

    zdt_file = klines_root / "extra" / "zdt" / f"{date_str}.csv"
    df = read_csv_safe(zdt_file, dtype=str, keep_default_na=False)
    if df is None or df.empty or "ts_code" not in df.columns:
        return []

    # 只保留涨停相关 (涨停池、连扳池)
    if "limit_type" in df.columns:
        df = df[df["limit_type"].isin(["涨停池", "连扳池"])]
    if df.empty:
        return []

    records: list[dict[str, object]] = []
    for _, row in df.iterrows():
        tag_val = row.get("tag", "")
        if (isinstance(tag_val, float) and math.isnan(tag_val)) or tag_val in (None, ""):
            tag_val = "首板"

        def _safe_float(val: object) -> float:
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0
        records.append({
            "ticker": str(row.get("ts_code", "")),
            "tag": str(tag_val),
            "board_type": str(row.get("status", "")),
            "limit_type": "涨停",
            "is_limit": True,
            "pct_chg": _safe_float(row.get("pct_chg", 0)),
            "limit_up_suc_rate": _safe_float(row.get("limit_up_suc_rate", 0)),
        })
    return records


def _get_workday_fallback(ref_date: str, n: int) -> list[str]:
    """生成工作日列表（周一至周五，排除五一、十一假期）。"""
    from datetime import datetime, timedelta

    end = datetime.strptime(ref_date, "%Y%m%d")
    days = []
    current = end

    # 往前推足够多天以获取 n 个工作日
    for _ in range(n * 2):
        date_str = current.strftime("%Y%m%d")

        # 跳过周末
        if current.weekday() >= 5:
            current -= timedelta(days=1)
            continue

        # 跳过五一假期 (0501-0505)
        if date_str[4:] >= "0501" and date_str[4:] <= "0505":
            current -= timedelta(days=1)
            continue

        # 跳过十一假期 (1001-1008)
        if date_str[4:] >= "1001" and date_str[4:] <= "1008":
            current -= timedelta(days=1)
            continue

        days.append(date_str)

        if len(days) >= n:
            break

        current -= timedelta(days=1)

    return sorted(days)
