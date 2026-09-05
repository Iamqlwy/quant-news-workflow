"""Session Builder —— 构建 SessionData。

负责从磁盘加载所有数据 → 归一化 → 组装 SessionData。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from src.market.data.cache import CacheManager
from src.market.data.loader import (
    get_recent_trading_days,
    load_all_concept_members,
    load_classification,
    load_daily_window,
    load_hk_basic,
    load_stock_basic,
    load_stock_name_history,
    load_zdt,
)
from src.market.types import DailyTicker, SessionData

# CSV 列 → DailyTicker 字段映射
_CSV_TO_DT: dict[str, str] = {
    "open": "open", "high": "high", "low": "low", "close": "close",
    "pre_close": "pre_close", "volume": "volume", "amount": "amount",
    "turnover_rate": "turnover_rate", "pe": "pe", "pb": "pb",
    "float_share": "float_share", "total_mv": "total_mv", "circ_mv": "circ_mv",
}


def _row_to_daily_ticker(row: Any, ts_ms: int) -> DailyTicker:
    """Convert a row (from itertuples) to DailyTicker using getattr for safety."""
    def g(k: str, d: float = 0.0) -> Any:
        return getattr(row, k, d)
    return DailyTicker(
        ts_code=str(g("ts_code", "")),
        timestamp=ts_ms,
        open=g("open"),
        high=g("high"),
        low=g("low"),
        close=g("close"),
        pre_close=g("pre_close"),
        volume=g("volume"),
        amount=g("amount"),
        volume_ratio=g("volume_ratio"),
        turnover_rate=g("turnover_rate"),
        turnover_rate_f=g("turnover_rate_f"),
        pe=g("pe"),
        pe_ttm=g("pe_ttm"),
        pb=g("pb"),
        ps=g("ps"),
        ps_ttm=g("ps_ttm"),
        dv_ratio=g("dv_ratio"),
        dv_ttm=g("dv_ttm"),
        total_share=g("total_share"),
        float_share=g("float_share"),
        free_share=g("free_share"),
        total_mv=g("total_mv"),
        circ_mv=g("circ_mv"),
    )


def _daily_to_ticker_dicts(
    df: pd.DataFrame, target_date: str,
) -> dict[str, DailyTicker]:
    """Extract per-ticker DailyTicker from merged daily DataFrame."""
    if df is None or df.empty or "ts_code" not in df.columns:
        return {}

    ts_col = df["timestamp"]
    if pd.api.types.is_datetime64_any_dtype(ts_col):
        mask = ts_col.dt.strftime("%Y%m%d") == target_date
    else:
        mask = pd.to_datetime(ts_col, errors="coerce").dt.strftime("%Y%m%d") == target_date

    subset = df[mask]
    if subset.empty:
        return {}

    ts_ms = int(pd.Timestamp(target_date).timestamp() * 1000)

    result: dict[str, DailyTicker] = {}
    for row in subset.itertuples(index=False):
        result[str(row.ts_code)] = _row_to_daily_ticker(row, ts_ms)
    return result


def _split_1m_by_date(
    df: pd.DataFrame, yesterday: str, today: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, object], dict[str, object], dict[str, object]]:
    """Split merged 1m DataFrame by date using vectorized epoch_days comparison."""
    if df is None or df.empty or "ticker" not in df.columns:
        return {}, {}, {}, {}, {}

    ts = df["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(ts):
        ts = pd.to_datetime(ts, errors="coerce")
    epoch_days = ts.astype("datetime64[ns]").astype("int64") // 86_400_000_000_000

    yesterday_d = int(pd.Timestamp(yesterday).timestamp() // 86400)
    today_d = int(pd.Timestamp(today).timestamp() // 86400)

    y_mask = epoch_days == yesterday_d
    t_mask = epoch_days == today_d

    keep_cols = [c for c in df.columns if c not in ("ticker",)]
    last: dict[str, pd.DataFrame] = {}
    today_map: dict[str, pd.DataFrame] = {}
    close_arrays: dict[str, object] = {}
    cum_amt_arrays: dict[str, object] = {}
    amt_arrays: dict[str, object] = {}

    df_y = df.loc[y_mask]
    for ticker, grp in df_y.groupby("ticker", sort=False):
        last[str(ticker)] = grp[keep_cols].reset_index(drop=True)

    df_t = df.loc[t_mask]
    for ticker, grp in df_t.groupby("ticker", sort=False):
        tkr = str(ticker)
        df_ticker = grp[keep_cols].reset_index(drop=True)
        today_map[tkr] = df_ticker
        if "close" in df_ticker.columns and "amount" in df_ticker.columns:
            close_arrays[tkr] = df_ticker["close"].to_numpy(dtype=float, copy=False)
            amt_arr = df_ticker["amount"].to_numpy(dtype=float, copy=False)
            amt_arrays[tkr] = amt_arr
            cum_amt_arrays[tkr] = np.cumsum(amt_arr)

    return last, today_map, close_arrays, cum_amt_arrays, amt_arrays


def _find_1m_tickers(klines_path: Path) -> list[str]:
    """扫描 1m/ 目录获取所有 ticker 列表。"""
    m1_dir = klines_path / "1m"
    if not m1_dir.exists():
        return []
    return sorted(p.stem for p in m1_dir.glob("*.csv"))


def build_session(cache: CacheManager, klines_path: Path, today_str: str) -> SessionData:
    """构建完整的 SessionData。

    参数：
        cache: 缓存管理器
        klines_path: 数据根目录
        today_str: 今日日期 YYYYMMDD

    返回：填充了所有字段的 SessionData。
    """
    t_total = time.perf_counter()
    session = SessionData()

    # ── 1. 获取交易日窗口 ──
    t0 = time.perf_counter()
    trading_days = get_recent_trading_days(klines_path, today_str, n=30)
    logger.debug("[timing] 1. trading_days: {:.2f}s", time.perf_counter() - t0)
    if not trading_days:
        return session
    session.daily_window = trading_days

    # ── 2. 确定日期 ──
    yesterday = trading_days[-2] if len(trading_days) >= 2 else trading_days[-1]

    dates_to_load = [yesterday]
    today_file = klines_path / "extra" / "all_stocks_daily" / f"{today_str}.csv"
    has_today_csv = today_file.exists()
    if has_today_csv:
        dates_to_load.append(today_str)

    # ── 3. 加载日线 → last_daily_ticker / today_daily_ticker ──
    t0 = time.perf_counter()
    consolidated = load_daily_window(klines_path, dates_to_load)
    logger.debug("[timing] 2. load_daily_window (n={}): {:.2f}s", len(dates_to_load), time.perf_counter() - t0)

    if not consolidated.empty:
        t0 = time.perf_counter()
        session.last_daily_ticker = _daily_to_ticker_dicts(consolidated, yesterday)
        logger.debug("[timing] 3. last_daily ({} tickers): {:.2f}s",
                  len(session.last_daily_ticker), time.perf_counter() - t0)

        if has_today_csv:
            t0 = time.perf_counter()
            session.today_daily_ticker = _daily_to_ticker_dicts(consolidated, today_str)
            logger.debug("[timing] 4. today_daily ({} tickers): {:.2f}s",
                      len(session.today_daily_ticker), time.perf_counter() - t0)

    # ── 5. 并行加载 1m 数据 + 元数据 ──
    tickers = list(session.last_daily_ticker.keys()) if session.last_daily_ticker else _find_1m_tickers(klines_path)
    index_start_date = f"{yesterday[:4]}-{yesterday[4:6]}-{yesterday[6:]}" if len(yesterday) == 8 else yesterday

    close_arrs: dict[str, object] = {}
    cum_amt_arrs: dict[str, object] = {}
    amt_arrs: dict[str, object] = {}

    def _load_1m_and_split() -> None:
        nonlocal close_arrs, cum_amt_arrs, amt_arrs
        t0 = time.perf_counter()
        try:
            from src.market.data.indexer import load_1m_bulk_df
            from src.market.data.loader import load_index_1m
            from src.market.data.normalizer import apply_intraday_units

            # 股票 1m
            df_1m = load_1m_bulk_df(tickers, klines_path, index_start_date, n_days=2, workers=32)
            logger.debug("[timing] 5a. load_1m_bulk_df ({} tickers): {:.2f}s", len(tickers), time.perf_counter() - t0)
            if df_1m is not None and not df_1m.empty:
                t0 = time.perf_counter()
                df_1m = apply_intraday_units(df_1m)
                session.last_1m_ticker, session.today_1m_ticker, close_arrs, cum_amt_arrs, amt_arrs = _split_1m_by_date(
                    df_1m, yesterday, today_str,
                )
                session.adhoc["_close_arrays"] = close_arrs
                session.adhoc["_cum_amt_arrays"] = cum_amt_arrs
                session.adhoc["_amt_arrays"] = amt_arrs
                logger.debug("[timing] 5b. split_1m (stock last={} today={}): {:.2f}s",
                          len(session.last_1m_ticker), len(session.today_1m_ticker),
                          time.perf_counter() - t0)

            # 指数 1m —— 合并到同一个字典，key 为 index_code
            # 注意：load_index_1m 返回前已调用 apply_intraday_units，不重复归一化
            t0 = time.perf_counter()
            df_idx = load_index_1m(klines_path, since_date=yesterday)
            if df_idx is not None and not df_idx.empty and "index_code" in df_idx.columns:
                df_idx = df_idx.rename(columns={"index_code": "ticker"})
                idx_last, idx_today, idx_close, idx_cum_amt, idx_amt = _split_1m_by_date(df_idx, yesterday, today_str)
                session.last_1m_ticker.update(idx_last)
                session.today_1m_ticker.update(idx_today)
                close_arrs.update(idx_close)
                cum_amt_arrs.update(idx_cum_amt)
                amt_arrs.update(idx_amt)
                logger.debug("[timing] 5c. index_1m (last={} today={}): {:.2f}s",
                          len(idx_last), len(idx_today), time.perf_counter() - t0)
        except Exception as e:
            logger.warning("1m data load failed: {}", e)

    def _load_meta_all() -> None:
        t0 = time.perf_counter()
        session.classification = load_classification(klines_path)

        # 从 classification 提取有效概念 code，只加载这些概念的 member 和 kline
        valid_codes: set[str] = set()
        for key in ["concept", "industry", "region"]:
            df_cls = session.classification.get(key)
            if df_cls is not None and not df_cls.empty:
                valid_codes.update(df_cls.iloc[:, 0].astype(str).str.replace(".TI", "").tolist())
        df_ic = session.classification.get("industry_children")
        if df_ic is not None and not df_ic.empty:
            for col in ["child_ts_code", "parent_ts_code"]:
                if col in df_ic.columns:
                    valid_codes.update(df_ic[col].astype(str).str.replace(".TI", "").tolist())

        session.adhoc["_classification_codes"] = valid_codes
        session.all_members = load_all_concept_members(klines_path, workers=8, valid_codes=valid_codes)
        session.stock_basic = load_stock_basic(klines_path)
        session.hk_basic = load_hk_basic(klines_path)
        session.stock_name_history = load_stock_name_history(klines_path)
        logger.debug("[timing] 6. meta: {:.2f}s", time.perf_counter() - t0)

    def _load_volume_5d() -> None:
        t0 = time.perf_counter()
        prev_5_days = [d for d in session.daily_window if d < yesterday][-5:]
        volume_5d: dict[str, list[float]] = {}
        for date_str in reversed(prev_5_days):
            csv_path = klines_path / "extra" / "all_stocks_daily" / f"{date_str}.csv"
            if not csv_path.exists():
                continue
            try:
                raw = pd.read_csv(csv_path, usecols=["ts_code", "vol"], dtype={"ts_code": str, "vol": float})
                raw["vol"] = raw["vol"] / 100.0
                for tkr, vol in zip(raw["ts_code"], raw["vol"], strict=True):
                    volume_5d.setdefault(tkr, []).append(float(vol))
            except Exception:
                pass
        session.adhoc["volume_5d"] = volume_5d
        logger.debug("[timing] 7. volume_5d ({} dates, {} tickers): {:.2f}s",
                  len(prev_5_days), len(volume_5d), time.perf_counter() - t0)

    def _load_zdt() -> None:
        t0 = time.perf_counter()
        # 今日 ZDT（可能存在也可能不存在）
        session.zdt_today = load_zdt(klines_path, today_str) or None
        # 昨日 ZDT
        prev_days = [d for d in session.daily_window if d < today_str]
        if len(prev_days) >= 1:
            session.zdt_yesterday = load_zdt(klines_path, prev_days[-1]) or None
        if len(prev_days) >= 2:
            session.zdy_before_yesterday = load_zdt(klines_path, prev_days[-2]) or None
        logger.debug("[timing] 8. zdt (today={} yesterday={} before_yesterday={}): {:.2f}s",
                  len(session.zdt_today or []),
                  len(session.zdt_yesterday or []),
                  len(session.zdy_before_yesterday or []),
                  time.perf_counter() - t0)

    t_parallel = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_1m = ex.submit(_load_1m_and_split)
        f_meta = ex.submit(_load_meta_all)
        f_vol = ex.submit(_load_volume_5d)
        f_zdt = ex.submit(_load_zdt)
        f_1m.result()
        f_meta.result()
        f_vol.result()
        f_zdt.result()

    logger.debug("[timing] 5-8. parallel phase: {:.2f}s", time.perf_counter() - t_parallel)

    # ── 8. 预计算概念列表 + 板块名称→代码索引 ──
    t0 = time.perf_counter()
    concept_list: list[dict] = []
    sector_name_to_code: dict[str, str] = {}
    for key, df in session.classification.items():
        if df is not None and not df.empty:
            for row in df.itertuples(index=False):
                code = str(row[0])
                name = str(row[1]) if len(row) > 1 else ""
                concept_list.append({"code": code, "name": name, "type": key})
                if name:
                    sector_name_to_code[name] = code
    session.adhoc["concept_list"] = concept_list
    session.adhoc["sector_name_to_code"] = sector_name_to_code
    logger.debug("[timing] 8. precompute indexes (concepts={} sectors={}): {:.2f}s",
              len(concept_list), len(sector_name_to_code), time.perf_counter() - t0)

    # ── 9. 预计算板块 1m bars（用流通市值加权平均） ──
    t0 = time.perf_counter()
    try:
        from src.market.compute.sector import compute_sector_bars
        from src.market.data.loader import load_concept_kline

        if session.last_1m_ticker and session.all_members is not None and not session.all_members.empty:
            # 合并 1m 数据
            all_1m_parts = []
            for tkr, df_part in session.last_1m_ticker.items():
                df_p = df_part.copy()
                df_p["ticker"] = tkr
                all_1m_parts.append(df_p)
            if session.today_1m_ticker:
                for tkr, df_part in session.today_1m_ticker.items():
                    df_p = df_part.copy()
                    df_p["ticker"] = tkr
                    all_1m_parts.append(df_p)

            if all_1m_parts:
                df_1m_merged = pd.concat(all_1m_parts, ignore_index=True)

                # 从 consolidated 提取昨日收盘和权重
                ts_col = pd.to_datetime(consolidated["timestamp"], errors="coerce")
                yesterday_ts = pd.Timestamp(yesterday)
                mask = ts_col <= yesterday_ts
                last_daily = consolidated[mask].sort_values("timestamp").groupby("ts_code").last().reset_index()
                prev_close_map = last_daily.set_index("ts_code")["close"].to_dict()
                weight_map = last_daily.set_index("ts_code")["circ_mv"].to_dict()

                # 从缓存加载概念日线，提取昨日板块收盘
                valid_codes = session.adhoc.get("_classification_codes")
                concept_daily = load_concept_kline(klines_path, days=30, valid_codes=valid_codes)
                concept_yesterday_close: dict[str, float] = {}
                if concept_daily is not None and not concept_daily.empty and "ts_code" in concept_daily.columns:
                    cd_ts = pd.to_datetime(concept_daily["timestamp"], errors="coerce")
                    cd_mask = cd_ts <= yesterday_ts
                    cd_last = concept_daily[cd_mask].sort_values("timestamp").groupby("ts_code").last().reset_index()
                    # 统一去除 .TI 后缀，与 all_members.con_code 保持一致
                    concept_yesterday_close = {
                        str(k).replace(".TI", ""): float(v)
                        for k, v in cd_last.set_index("ts_code")["close"].to_dict().items()
                    }
                session.adhoc["_concept_yesterday_close"] = concept_yesterday_close

                # 计算板块 1m bars
                sector_bars = compute_sector_bars(
                    df_1m_merged, prev_close_map, weight_map, concept_yesterday_close, session.all_members,
                )
                sector_bars_ti = {f"{k}.TI" if not k.endswith(".TI") else k: v for k, v in sector_bars.items()}

                # 按日期拆分后合并进 last_1m_ticker / today_1m_ticker，使 get_live_1m 自动可用
                for code_ti, df_sec in sector_bars_ti.items():
                    df_sec = df_sec.copy()
                    df_sec["_date"] = pd.to_datetime(df_sec["timestamp"], errors="coerce").dt.strftime("%Y%m%d")
                    df_y = df_sec[df_sec["_date"] == yesterday].drop(columns=["_date"]).reset_index(drop=True)
                    df_t = df_sec[df_sec["_date"] == today_str].drop(columns=["_date"]).reset_index(drop=True)
                    if not df_y.empty:
                        session.last_1m_ticker[code_ti] = df_y
                    if not df_t.empty:
                        session.today_1m_ticker[code_ti] = df_t

                logger.debug("[timing] 9. sector_bars (concepts={}): {:.2f}s",
                          len(sector_bars), time.perf_counter() - t0)
    except Exception as e:
        logger.warning("sector 1m bars precompute failed: {}", e)

    # ── 10. 存储 klines_path 供后续 loader 兜底 ──
    session.adhoc["klines_root"] = klines_path
    session.adhoc["loaded_date"] = today_str

    logger.info("[timing] TOTAL build_session: {:.2f}s", time.perf_counter() - t_total)
    return session
