"""板块 Bar 预计算 —— 从全市场 1m 数据计算概念板块加权 K 线。

使用流通市值加权平均计算板块涨跌幅，再乘以昨日板块收盘还原绝对价格。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_sector_bars(
    df_1m: pd.DataFrame,
    prev_close_map: dict[str, float],
    weight_map: dict[str, float],
    concept_yesterday_close: dict[str, float],
    all_members: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """计算所有概念的板块加权 K 线。

    算法：
    1. 计算每只股票 1m 涨跌幅 = (1m_close / 昨日收盘 - 1)
    2. 用流通市值加权平均涨跌幅
    3. 用概念昨日收盘价 × (1 + 加权涨跌幅) 还原绝对价格

    参数：
        df_1m: 全市场 1m 数据，需含 ticker、timestamp、close、volume、amount 列
        prev_close_map: {ts_code: 昨日收盘价}
        weight_map: {ts_code: 流通市值}
        concept_yesterday_close: {con_code（无 .TI 后缀）: 昨日板块收盘价}
        all_members: 概念成员关系，需含 con_code 和 ts_code 列

    返回：{concept_code: DataFrame}，每个 DataFrame 含 timestamp、close、volume、amount、con_code 列。
    """
    if df_1m.empty or all_members.empty or not concept_yesterday_close:
        return {}

    # ── 1. 将权重和昨日收盘映射到 1m 数据 ──
    df = df_1m.copy()
    df["weight"] = df["ticker"].map(weight_map)
    df["prev_close"] = df["ticker"].map(prev_close_map)

    # 过滤：只保留有有效权重和昨日收盘的股票
    # 权重校验：剔除非正值和异常值，防止负市值或极大值污染加权
    df["weight"] = df["weight"].clip(lower=0.0)
    total_weight_before = df["weight"].sum()
    df = df[df["weight"].notna() & (df["weight"] > 0) & df["prev_close"].notna()]
    if df.empty:
        return {}

    # 检查有效权重占比，过低时记录告警（<50% 说明大量成员权重缺失）
    if total_weight_before > 0:
        coverage = df["weight"].sum() / total_weight_before
        if coverage < 0.5:
            # 权重覆盖率过低，计算结果可能不准确
            pass  # TODO: 接入日志后加 warning

    # ── 2. 计算每只股票的 1m 涨跌幅 ──
    df["pct_chg"] = (df["close"] / df["prev_close"] - 1.0)

    # 去重（同一 ticker+timestamp 取最新）
    df = df.drop_duplicates(subset=["ticker", "timestamp"], keep="last")

    # ── 3. 为时间和股票创建整数索引 ──
    tickers = df["ticker"].unique()
    times = df["timestamp"].unique()
    ticker_to_idx = {t: i for i, t in enumerate(tickers)}
    time_to_idx = {t: i for i, t in enumerate(times)}

    T = len(times)
    K = len(tickers)

    # ── 4. 构建密集矩阵 ──
    pct_mat = np.full((T, K), np.nan)
    vol_mat = np.zeros((T, K))
    amt_mat = np.zeros((T, K))
    weight_arr = np.zeros(K)

    for tkr, idx in ticker_to_idx.items():
        weight_arr[idx] = weight_map.get(tkr, 0.0)

    # 向量化填充矩阵
    t_idx = df["timestamp"].map(time_to_idx).to_numpy(dtype=int)
    k_idx = df["ticker"].map(ticker_to_idx).to_numpy(dtype=int)
    pct_mat[t_idx, k_idx] = df["pct_chg"].to_numpy(dtype=float)
    if "volume" in df.columns:
        vol_mat[t_idx, k_idx] = df["volume"].to_numpy(dtype=float)
    if "amount" in df.columns:
        amt_mat[t_idx, k_idx] = df["amount"].to_numpy(dtype=float)

    # ── 5. 按概念分组计算加权平均涨跌幅 ──
    result: dict[str, pd.DataFrame] = {}
    concept_to_members = all_members.groupby("con_code")["ts_code"].apply(list).to_dict()  # type: ignore[union-attr]

    for concept_code, member_codes in concept_to_members.items():
        base_close = concept_yesterday_close.get(concept_code)
        if base_close is None or base_close <= 0:
            continue

        member_indices = [ticker_to_idx.get(m, -1) for m in member_codes]
        member_indices = [i for i in member_indices if i >= 0]
        if not member_indices:
            continue

        idx_arr = np.array(member_indices, dtype=int)
        w = weight_arr[idx_arr]
        if not (w > 0).any():
            continue

        # 加权平均 pct_chg（仅有效成员参与）
        pct_slice = pct_mat[:, idx_arr]
        valid_mask = ~np.isnan(pct_slice)
        pct_sub = np.where(valid_mask, pct_slice, 0.0)
        vol_sub = vol_mat[:, idx_arr]
        amt_sub = amt_mat[:, idx_arr]

        weighted_pct_sum = pct_sub @ w
        valid_count = valid_mask @ w
        weighted_pct = np.divide(weighted_pct_sum, valid_count, where=valid_count > 0, out=np.full(T, np.nan))

        # 还原绝对价格：base_close * (1 + weighted_pct)
        weighted_close = base_close * (1.0 + weighted_pct)

        # 总和 volume/amount
        total_vol = vol_sub.sum(axis=1)
        total_amt = amt_sub.sum(axis=1)

        # 构建 DataFrame（补全 open/high/low 与 stock 1m 列结构一致）
        out = pd.DataFrame({
            "timestamp": times,
            "open": weighted_close,
            "high": weighted_close,
            "low": weighted_close,
            "close": weighted_close,
            "volume": total_vol,
            "amount": total_amt,
        })
        out["con_code"] = concept_code
        out = out.dropna(subset=["close"])

        if not out.empty:
            result[concept_code] = out.sort_values("timestamp").reset_index(drop=True)

    return result
