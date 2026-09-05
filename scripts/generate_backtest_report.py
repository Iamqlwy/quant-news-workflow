#!/usr/bin/env python
"""综合回测评估报告生成器。

从 quant_kb 数据库读取所有 trading_operations，结合本地日线数据，
生成多维度的回测评估报告（终端输出 + 可视化图表 + Markdown 文件）。

覆盖维度：
  A. 组合层面指标（净值曲线、Sharpe/Sortino/Calmar、最大回撤）
  B. 交易信号分析（T+N收益分布、MFE/MAE、IC分析）
  C. 风控诊断（风险分层、连续亏损、止损效果）
  D. 择时/市场环境（牛熊分层、滚动胜率、月度收益）
  E. 行业/板块暴露（概念收益汇总、集中度）
  F. 决策流程诊断（approved vs rejected、skip质量、sell时机）
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import psycopg2

# ── 配置 ──────────────────────────────────────────────────────────────
DB_NAME = "quant_kb"
DB_HOST = "localhost"
DB_PORT = 15432
DB_USER = "postgres"
DB_PASSWORD = "postgres"

KLINES_DIR = Path("C:/klines/daily")
INDEX_DIR = Path("C:/klines/index_daily")
CONCEPTS_DIR = Path("C:/klines/concepts/kline")
STOCK_CONCEPTS_PATH = Path("C:/klines/concepts/stock_concepts.csv")
EXTRA_DIR = Path("C:/klines/extra/all_stocks_daily")

HORIZONS = [1, 3, 5, 10, 20, 30]
# 基准指数映射：使用实际文件名
BENCHMARKS = [
    ("创业板指", "创业板指"),
    ("中证500", "中证500"),
    ("沪深300", "沪深300"),
]
RISK_FREE_RATE = 0.02  # 2% 无风险利率（年化）

CHARTS_DIR = Path(__file__).resolve().parent / "charts"
CHARTS_DIR.mkdir(exist_ok=True)
NOW = datetime.now().strftime("%Y%m%d_%H%M%S")
REPORT_PATH = Path(__file__).resolve().parent / f"backtest_report_{NOW}.md"

# ── Matplotlib 配置 ───────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.dates import DateFormatter, AutoDateLocator
import seaborn as sns

sns.set_theme(style="whitegrid", context="talk")
plt.rcParams.update({
    "font.sans-serif": ["SimHei", "Microsoft YaHei", "DejaVu Sans"],
    "axes.unicode_minus": False,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
})

COLORS = sns.color_palette("Set2", 8)
RED = "#E74C3C"
GREEN = "#27AE60"
BLUE = "#2980B9"
ORANGE = "#E67E22"
PURPLE = "#8E44AD"
GRAY = "#95A5A6"
BENCH_COLORS = {"创业板指": BLUE, "中证500": ORANGE, "沪深300": GREEN}
BENCH_LINESTYLES = {"创业板指": "-", "中证500": "--", "沪深300": "-."}


# ════════════════════════════════════════════════════════════════════════
# 数据库查询
# ════════════════════════════════════════════════════════════════════════

def fetch_operations(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, operation_type, symbol, created_at, status,
               rationale, risk_level, trigger_analysis_id, price, quantity
        FROM trading_operations
        ORDER BY created_at
    """)
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": str(r[0]), "operation_type": r[1], "symbol": r[2],
            "created_at": r[3], "status": r[4], "rationale": r[5],
            "risk_level": r[6], "trigger_analysis_id": str(r[7]) if r[7] else None,
            "price": float(r[8]) if r[8] else None,
            "quantity": float(r[9]) if r[9] else None,
        })
    cur.close()
    return rows


def fetch_analyses(conn) -> dict[str, dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, confidence, time_horizon, analysis_type
        FROM analyses WHERE confidence IS NOT NULL
    """)
    result = {}
    for r in cur.fetchall():
        result[str(r[0])] = {
            "confidence": float(r[1]) if r[1] is not None else None,
            "time_horizon": r[2], "analysis_type": r[3],
        }
    cur.close()
    return result


def fetch_feedbacks(conn) -> list[dict]:
    """返回 feedback 列表，用于判断哪些 trade 有事后评价。"""
    cur = conn.cursor()
    cur.execute("SELECT id, trigger_analysis_id, judgment_correct FROM feedbacks")
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": str(r[0]),
            "trigger_analysis_id": str(r[1]) if r[1] else None,
            "judgment_correct": r[2],
        })
    cur.close()
    return rows


# ════════════════════════════════════════════════════════════════════════
# 日线数据工具
# ════════════════════════════════════════════════════════════════════════

def _normalize_date(d: str) -> str:
    d = d.strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _to_date(dt):
    if hasattr(dt, "date"):
        return dt.date()
    return dt


def _effective_buy_date(created_at) -> str:
    d = _to_date(created_at)
    if hasattr(created_at, "hour") and created_at.hour >= 15:
        d = d + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def load_klines(symbol: str) -> dict[str, dict]:
    """返回 {YYYY-MM-DD: {open, high, low, close, pct_chg}}。"""
    path = KLINES_DIR / f"{symbol}.csv"
    if not path.exists():
        path = INDEX_DIR / f"{symbol}.csv"
    if not path.exists():
        return {}
    data: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = _normalize_date(row["trade_date"])
            data[d] = {
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "pct_chg": float(row["pct_chg"]),
            }
    return data


def _sorted_dates(klines: dict[str, dict]) -> list[str]:
    return sorted(klines.keys())


def get_close_on_or_after(
    dates: list[str], klines: dict[str, dict], target_date: str
) -> tuple[Optional[float], Optional[str], Optional[int]]:
    for i, d in enumerate(dates):
        if d >= target_date:
            return klines[d]["close"], d, i
    return None, None, None


def get_next_n_returns(
    dates: list[str], klines: dict[str, dict],
    start_idx: int, buy_price: float,
) -> dict[int, Optional[dict]]:
    """计算买入后 n 个交易日的收益率、MFE、MAE。"""
    result = {}
    for h in HORIZONS:
        end_idx = start_idx + h
        if end_idx >= len(dates):
            result[h] = None
            continue
        end_close = klines[dates[end_idx]]["close"]
        ret_pct = (end_close - buy_price) / buy_price * 100
        max_price = buy_price
        min_price = buy_price
        for j in range(start_idx, end_idx + 1):
            price = klines[dates[j]]["close"]
            if price > max_price:
                max_price = price
            if price < min_price:
                min_price = price
        mfe = (max_price - buy_price) / buy_price * 100
        mae = (min_price - buy_price) / buy_price * 100
        result[h] = {
            "return_pct": round(ret_pct, 2),
            "mfe_pct": round(mfe, 2),
            "mae_pct": round(mae, 2),
            "end_date": dates[end_idx],
        }
    return result


def classify_market_state(
    index_klines: dict[str, dict], dates: list[str],
    target_date: str, lookback: int = 60,
) -> str:
    idx = None
    for i, d in enumerate(dates):
        if d >= target_date:
            idx = i
            break
    if idx is None or idx < lookback:
        return "unknown"
    start = max(0, idx - lookback)
    segment = [float(index_klines[dates[j]]["close"]) for j in range(start, idx)]
    if len(segment) < 20:
        return "unknown"
    total_ret = (segment[-1] - segment[0]) / segment[0] * 100
    if total_ret > 10:
        return "bull"
    elif total_ret < -10:
        return "bear"
    else:
        return "range"


# ════════════════════════════════════════════════════════════════════════
# 统计工具
# ════════════════════════════════════════════════════════════════════════

def _stats(arr: list[float]) -> dict:
    if not arr:
        return {"n": 0, "mean": 0, "median": 0, "std": 0, "min": 0, "max": 0}
    a = np.array(arr, dtype=float)
    return {
        "n": len(a), "mean": float(np.mean(a)), "median": float(np.median(a)),
        "std": float(np.std(a, ddof=1)), "min": float(np.min(a)),
        "max": float(np.max(a)),
    }


def sharpe_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    """年化夏普比率。"""
    if len(returns) < 2:
        return 0.0
    r = np.array(returns, dtype=float) / 100.0
    excess = r - RISK_FREE_RATE / periods_per_year
    if np.std(excess, ddof=1) == 0:
        return 0.0
    return float(np.mean(excess) / np.std(excess, ddof=1) * np.sqrt(periods_per_year))


def sortino_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    """年化索提诺比率（只惩罚下行波动）。"""
    if len(returns) < 2:
        return 0.0
    r = np.array(returns, dtype=float) / 100.0
    downside = r[r < 0]
    if len(downside) < 2:
        return 0.0 if len(r) > 0 else 0.0
    target = RISK_FREE_RATE / periods_per_year
    downside_std = np.std(downside - target, ddof=1)
    if downside_std == 0:
        return 0.0
    return float(np.mean(r - target) / downside_std * np.sqrt(periods_per_year))


def calmar_ratio(returns: list[float], max_drawdown_pct: float) -> float:
    """年化 Calmar 比率 = 年化收益 / 最大回撤（取绝对值）。"""
    if len(returns) < 2 or max_drawdown_pct == 0:
        return 0.0
    r = np.array(returns, dtype=float) / 100.0
    annual_return = np.mean(r) * 252
    return float(annual_return / (abs(max_drawdown_pct) / 100.0))


def max_drawdown_details(returns: list[float]) -> dict:
    """计算最大回撤及持续时间。"""
    if not returns:
        return {"mdd_pct": 0, "peak_date": None, "trough_date": None, "recovery_days": 0, "duration_days": 0}
    r = np.array(returns, dtype=float)
    cumulative = np.cumprod(1 + r / 100.0)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak * 100
    mdd_idx = np.argmin(drawdown)
    mdd_pct = float(drawdown[mdd_idx])
    peak_idx = np.argmax(cumulative[:mdd_idx + 1])
    # 恢复天数
    recovery_idx = None
    for i in range(mdd_idx + 1, len(drawdown)):
        if drawdown[i] >= 0:
            recovery_idx = i
            break
    return {
        "mdd_pct": round(mdd_pct, 2),
        "peak_idx": int(peak_idx),
        "trough_idx": int(mdd_idx),
        "recovery_days": int(recovery_idx - mdd_idx) if recovery_idx else len(returns) - mdd_idx,
        "duration_days": int(mdd_idx - peak_idx),
    }


# ════════════════════════════════════════════════════════════════════════
# 板块数据加载
# ════════════════════════════════════════════════════════════════════════

def load_stock_concepts() -> dict[str, list[str]]:
    """返回 {ts_code: [concept_name, ...]}。从 stock_concepts.csv 的 all_concepts 解析。

    键使用完整 ts_code (如600487.SH)，与 trading_operations.symbol 格式一致。
    """
    mapping: dict[str, list[str]] = defaultdict(list)
    if not STOCK_CONCEPTS_PATH.exists():
        return mapping
    with open(STOCK_CONCEPTS_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_code = (row.get("con_code") or "").strip()
            if not ts_code:
                continue
            all_concepts = row.get("all_concepts", "") or ""
            if all_concepts:
                for concept in all_concepts.split("|"):
                    concept = concept.strip()
                    if concept:
                        mapping[ts_code].append(concept)
    return mapping


def load_concept_klines(concept_name: str) -> dict[str, dict]:
    path = CONCEPTS_DIR / f"{concept_name}.csv"
    if not path.exists():
        return {}
    data: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = _normalize_date(row["trade_date"])
            data[d] = {"close": float(row["close"]), "pct_change": float(row.get("pct_change", row.get("pct_chg", 0)))}
    return data


# ════════════════════════════════════════════════════════════════════════
# 核心计算
# ════════════════════════════════════════════════════════════════════════

def compute_portfolio_daily_returns(
    buy_results: list[dict],
    klines_cache: dict[str, dict[str, dict]],
    dates_cache: dict[str, list[str]],
) -> tuple[list[float], list[str], dict[str, list[float]], dict[str, list[str]]]:
    """等权组合日收益序列 + 基准指数同期收益。

    每一天，计算所有已买入且未超过HORIZONS[-1]的标的的等权平均收益。
    返回：(组合日收益列表, 日期列表, {bench_name: [日收益]}, {bench_name: [日期]})
    """
    if not buy_results:
        return [], [], {}, {}

    # 确定交易日历（用 创业板指 的交易日）
    cy_dates = sorted(benchmark_dates.get("创业板指", []))
    if not cy_dates:
        # fallback: 用第一个标的的交易日
        first_klines = klines_cache.get(buy_results[0]["symbol"], {})
        cy_dates = sorted(first_klines.keys())

    # 为每笔买入生成持仓期间的日收益（从 buy_date 开始，到 buy_date + MAX_HORIZON 为止）
    MAX_H = max(HORIZONS)
    daily_contributions: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        sym = r["symbol"]
        klines = klines_cache.get(sym, {})
        dates = dates_cache.get(sym, [])
        if not dates:
            continue
        buy_date = r["buy_date"]
        _, _, idx = get_close_on_or_after(dates, klines, buy_date)
        if idx is None:
            continue
        # 从买入日到买入日+MAX_H，每天贡献日收益
        for j in range(idx, min(idx + MAX_H, len(dates) - 1)):
            d = dates[j]
            next_d = dates[j + 1]
            day_ret = (klines[next_d]["close"] - klines[d]["close"]) / klines[d]["close"] * 100
            daily_contributions[d].append(day_ret)

    # 聚合为等权日收益序列
    all_dates = sorted(daily_contributions.keys())
    portfolio_rets = []
    valid_dates = []
    for d in all_dates:
        rets = daily_contributions[d]
        if rets:
            portfolio_rets.append(np.mean(rets))
            valid_dates.append(d)

    # 基准同期收益
    bench_rets: dict[str, list[float]] = {}
    bench_dts: dict[str, list[str]] = {}
    for name, _ in BENCHMARKS:
        b_klines = benchmark_data.get(name, {})
        if not b_klines:
            continue
        b_rets = []
        b_dates_list = []
        for d in valid_dates:
            if d in b_klines:
                b_rets.append(b_klines[d]["pct_chg"])
                b_dates_list.append(d)
        bench_rets[name] = b_rets
        bench_dts[name] = b_dates_list

    return portfolio_rets, valid_dates, bench_rets, bench_dts


def compute_mfe_mae_ratio(results: list[dict], horizon: int) -> dict:
    mfes, maes, ratios = [], [], []
    for r in results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        mfe = h_data["mfe_pct"]
        mae = abs(h_data["mae_pct"])
        mfes.append(mfe)
        maes.append(mae)
        if mae > 0:
            ratios.append(mfe / mae)
    return {
        "mfe": _stats(mfes), "mae": _stats(maes),
        "ratio_mean": float(np.mean(ratios)) if ratios else 0,
        "ratio_median": float(np.median(ratios)) if ratios else 0,
    }


def compute_excess_returns(
    buy_results: list[dict], bench_klines: dict[str, dict],
    bench_dates: list[str], horizon: int,
) -> list[float]:
    excess = []
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        effective_date = r["effective_buy_date"]
        b_close, _, b_idx = get_close_on_or_after(bench_dates, bench_klines, effective_date)
        if b_close is None or b_idx is None:
            continue
        b_rets = get_next_n_returns(bench_dates, bench_klines, b_idx, b_close)
        if b_rets.get(horizon) is None:
            continue
        excess.append(h_data["return_pct"] - b_rets[horizon]["return_pct"])
    return excess


def compute_rank_ic(scores: np.ndarray, forward_returns: np.ndarray) -> Optional[float]:
    mask = ~np.isnan(scores) & ~np.isnan(forward_returns)
    if mask.sum() < 10:
        return None
    from scipy.stats import spearmanr
    corr, _ = spearmanr(scores[mask], forward_returns[mask])
    return float(corr)


def compute_ic_sequence(
    buy_results: list[dict], analyses_map: dict[str, dict],
) -> dict[int, dict]:
    scores = []
    rets_by_h: dict[int, list[float]] = defaultdict(list)
    for r in buy_results:
        score = None
        if r["trigger_analysis_id"] and r["trigger_analysis_id"] in analyses_map:
            score = analyses_map[r["trigger_analysis_id"]].get("confidence")
        if score is None:
            continue
        scores.append(score)
        for h in HORIZONS:
            h_data = r.get(f"h_{h}")
            rets_by_h[h].append(h_data["return_pct"] if h_data else None)
    scores_arr = np.array(scores, dtype=float)
    result = {}
    for h in HORIZONS:
        rets_arr = np.array(rets_by_h[h], dtype=float)
        ic = compute_rank_ic(scores_arr, rets_arr)
        result[h] = {"ic": ic, "n": len(scores_arr)}
    return result


def compute_consecutive_losses(returns: list[float]) -> dict:
    """计算最大连续亏损次数。"""
    max_streak, cur_streak = 0, 0
    streaks = []
    for r in returns:
        if r < 0:
            cur_streak += 1
        else:
            if cur_streak > 0:
                streaks.append(cur_streak)
            cur_streak = 0
    if cur_streak > 0:
        streaks.append(cur_streak)
    max_streak = max(streaks) if streaks else 0
    return {
        "max_consecutive_losses": max_streak,
        "avg_consecutive_losses": float(np.mean(streaks)) if streaks else 0,
    }


# ════════════════════════════════════════════════════════════════════════
# 可视化图表
# ════════════════════════════════════════════════════════════════════════

def plot_portfolio_equity(
    portfolio_rets: list[float], dates: list[str],
    bench_rets: dict[str, list[float]], bench_dts: dict[str, list[str]],
):
    """图1：组合净值 vs 基准。"""
    fig, ax = plt.subplots(figsize=(16, 8))

    # 组合净值
    cum_ret = np.cumprod(1 + np.array(portfolio_rets) / 100.0)
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    ax.plot(date_objs, cum_ret, color=RED, linewidth=2.5, label="等权组合", zorder=5)

    # 基准
    for name, _ in BENCHMARKS:
        b_rets = bench_rets.get(name, [])
        b_dates = bench_dts.get(name, [])
        if not b_rets:
            continue
        b_cum = np.cumprod(1 + np.array(b_rets) / 100.0)
        b_date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in b_dates]
        ax.plot(b_date_objs, b_cum, color=BENCH_COLORS[name],
                linestyle=BENCH_LINESTYLES[name], linewidth=2, label=name, alpha=0.8)

    ax.axhline(1, color="gray", linestyle=":", alpha=0.5)
    ax.set_xlabel("日期")
    ax.set_ylabel("净值")
    ax.set_title("组合净值曲线 vs 基准指数", fontsize=16, fontweight="bold")
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(AutoDateLocator())

    # 添加统计信息文本框
    total_ret = (cum_ret[-1] - 1) * 100
    ann_ret = (cum_ret[-1] ** (252 / len(portfolio_rets)) - 1) * 100 if len(portfolio_rets) > 0 else 0
    vol = np.std(portfolio_rets, ddof=1) * np.sqrt(252)
    sr = sharpe_ratio(portfolio_rets)
    mdd = max_drawdown_details(portfolio_rets)
    text = (f"总收益: {total_ret:+.2f}%\n年化收益: {ann_ret:+.2f}%\n年化波动: {vol:.2f}%\n"
            f"Sharpe: {sr:.2f}\n最大回撤: {mdd['mdd_pct']:.2f}%")
    ax.text(0.02, 0.05, text, transform=ax.transAxes, fontsize=10, va="bottom",
            bbox={"boxstyle": "round,pad=0.5", "facecolor": "white", "alpha": 0.85})

    fig.autofmt_xdate()
    fig.savefig(CHARTS_DIR / "01_portfolio_equity.png")
    plt.close(fig)
    print("  ✓ 01 组合净值曲线已保存")


def plot_return_heatmap(buy_results: list[dict]):
    """图2：T+N 收益率热力图。"""
    buy_sorted = sorted(buy_results, key=lambda r: r["created_at"])
    fig, ax = plt.subplots(figsize=(14, max(8, len(buy_sorted) * 0.18)))

    rows = []
    for r in buy_sorted:
        row = []
        for h in HORIZONS:
            h_data = r.get(f"h_{h}")
            row.append(h_data["return_pct"] if h_data else None)
        rows.append(row)

    if not rows:
        ax.text(0.5, 0.5, "数据不足", transform=ax.transAxes, ha="center", va="center")
        fig.savefig(CHARTS_DIR / "02_return_heatmap.png")
        plt.close(fig)
        return

    data = np.array(rows, dtype=float)
    vmax = max(abs(np.nanpercentile(data, 2)), abs(np.nanpercentile(data, 98)), 1)
    cmap = sns.diverging_palette(130, 10, as_cmap=True)
    labels_y = [f"{r['symbol'] or '?'} {r['created_at'].strftime('%m-%d')}" for r in buy_sorted]
    labels_x = [f"T+{h}" for h in HORIZONS]

    sns.heatmap(data, cmap=cmap, center=0, vmin=-vmax, vmax=vmax,
                xticklabels=labels_x, yticklabels=labels_y,
                annot=True, fmt=".1f", linewidths=0.5, linecolor="white",
                cbar_kws={"label": "收益率 (%)"}, ax=ax)
    ax.set_title("T+N 收益率热力图", fontsize=15, fontweight="bold")
    ax.set_xlabel("持有天数")

    fig.savefig(CHARTS_DIR / "02_return_heatmap.png", bbox_inches="tight")
    plt.close(fig)
    print("  ✓ 02 收益率热力图已保存")


def plot_mfe_mae_scatter(buy_results: list[dict], horizon: int = 20):
    """图3：MFE/MAE 散点图。"""
    fig, ax = plt.subplots(figsize=(10, 8))
    mfes, maes, symbols, colors_list = [], [], [], []
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        mfe = h_data["mfe_pct"]
        mae = h_data["mae_pct"]
        mfes.append(mfe)
        maes.append(mae)
        symbols.append(r["symbol"] or "?")
        if mfe > abs(mae) and mae > -5:
            colors_list.append(GREEN)
        elif mfe > abs(mae) and mae <= -5:
            colors_list.append(ORANGE)
        elif mfe <= abs(mae) and mae > -5:
            colors_list.append(BLUE)
        else:
            colors_list.append(RED)

    if not mfes:
        fig.savefig(CHARTS_DIR / "03_mfe_mae_scatter.png")
        plt.close(fig)
        return

    max_val = max(max(abs(m) for m in mfes), max(abs(m) for m in maes)) * 1.15
    mae_abs = [abs(m) for m in maes]

    ax.axhline(0, color="black", linewidth=0.5)
    ax.axvline(0, color="black", linewidth=0.5)
    ax.plot([0, max_val], [0, max_val], color="gray", linestyle="--", alpha=0.5, label="MFE = |MAE|")
    ax.scatter(mae_abs, mfes, c=colors_list, alpha=0.7, s=80, edgecolors="white", linewidth=0.5)

    ax.text(max_val * 0.75, max_val * 0.85, "高风险高回报", fontsize=10, color=ORANGE, ha="center", alpha=0.7)
    ax.text(max_val * 0.25, max_val * 0.85, "低风险高回报 ★理想", fontsize=10, color=GREEN, ha="center", alpha=0.7)
    ax.text(max_val * 0.25, max_val * 0.1, "低风险低回报", fontsize=10, color=BLUE, ha="center", alpha=0.7)
    ax.text(max_val * 0.75, max_val * 0.1, "高风险低回报 ✗淘汰", fontsize=10, color=RED, ha="center", alpha=0.7)

    ax.set_xlabel(f"|MAE| — 最大不利偏移 (T+{horizon}, %)")
    ax.set_ylabel(f"MFE — 最大有利偏移 (T+{horizon}, %)")
    ax.set_title(f"MFE / MAE 散点图 (N={len(mfes)}, T+{horizon})", fontsize=15, fontweight="bold")
    ax.set_xlim(-max_val * 0.02, max_val)
    ax.set_ylim(-max_val * 0.02, max_val)
    ax.legend(loc="upper left")
    ax.set_aspect("equal")

    if mfes:
        best_idx = np.argmax(mfes)
        worst_idx = np.argmin(maes)
        ax.annotate(symbols[best_idx], (mae_abs[best_idx], mfes[best_idx]),
                    textcoords="offset points", xytext=(8, 8), fontsize=8, color=BLUE)
        ax.annotate(symbols[worst_idx], (mae_abs[worst_idx], mfes[worst_idx]),
                    textcoords="offset points", xytext=(8, -12), fontsize=8, color=RED)

    fig.savefig(CHARTS_DIR / "03_mfe_mae_scatter.png")
    plt.close(fig)
    print("  ✓ 03 MFE/MAE 散点图已保存")


def plot_return_boxplots(buy_results: list[dict]):
    """图4：T+1/5/20 收益分布箱线图。"""
    target_horizons = [1, 5, 20]
    fig, axes = plt.subplots(1, 3, figsize=(18, 7))
    for ax, h in zip(axes, target_horizons):
        rets = [r[f"h_{h}"]["return_pct"] for r in buy_results if r.get(f"h_{h}") is not None]
        if not rets:
            continue
        bp = ax.boxplot(rets, widths=0.4, patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2},
                        flierprops={"marker": "o", "markerfacecolor": RED, "alpha": 0.5})
        bp["boxes"][0].set_facecolor(BLUE)
        bp["boxes"][0].set_alpha(0.3)
        jitter = np.random.normal(0, 0.06, len(rets))
        ax.scatter(np.ones(len(rets)) + jitter, rets, alpha=0.3, s=15, color=GRAY)
        ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
        med = np.median(rets)
        mean = np.mean(rets)
        wr = sum(1 for x in rets if x > 0) / len(rets) * 100
        ax.text(0.6, ax.get_ylim()[1] * 0.95,
                f"均值: {mean:+.2f}%\n中位数: {med:+.2f}%\n胜率: {wr:.0f}%\nN={len(rets)}",
                fontsize=10, va="top", bbox={"boxstyle": "round,pad=0.4", "facecolor": "white", "alpha": 0.8})
        ax.set_title(f"T+{h} 收益分布", fontsize=14, fontweight="bold")
        ax.set_ylabel("收益率 (%)")
        ax.set_xticklabels([])
    fig.suptitle("未来N日收益分布箱线图", fontsize=16, fontweight="bold", y=1.01)
    fig.tight_layout()
    fig.savefig(CHARTS_DIR / "04_return_boxplots.png")
    plt.close(fig)
    print("  ✓ 04 收益分布箱线图已保存")


def plot_ic_decay(ic_sequence: dict[int, dict]):
    """图5：IC 衰减曲线。"""
    hs = sorted(ic_sequence.keys())
    ics = [ic_sequence[h]["ic"] for h in hs]
    valid_hs = [h for h, ic in zip(hs, ics) if ic is not None]
    valid_ics = [ic for ic in ics if ic is not None]
    if not valid_ics:
        print("  ⚠ 无有效 IC 数据，跳过 IC 衰减图")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(valid_hs, valid_ics, marker="o", linewidth=2.5, markersize=10, color=BLUE)
    for h, ic in zip(valid_hs, valid_ics):
        ax.annotate(f"{ic:.3f}", (h, ic), textcoords="offset points",
                    xytext=(0, 12), fontsize=9, ha="center", color=BLUE)
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.fill_between(valid_hs, 0, valid_ics, alpha=0.1, color=BLUE)
    ax.set_xlabel("预测周期 (交易日)")
    ax.set_ylabel("Rank IC")
    ax.set_title("信息系数 (IC) 衰减曲线\n信号强度 vs 未来超额收益", fontsize=15, fontweight="bold")
    ax.set_xticks(HORIZONS)
    fig.savefig(CHARTS_DIR / "05_ic_decay.png")
    plt.close(fig)
    print("  ✓ 05 IC 衰减曲线已保存")


def plot_rolling_win_rate(buy_results: list[dict], window: int = 60, horizon: int = 20):
    """图6：滚动胜率/盈亏比。"""
    buy_sorted = sorted(buy_results, key=lambda r: r["created_at"])
    if len(buy_sorted) < window:
        print(f"  ⚠ 样本不足 ({len(buy_sorted)} < {window})，跳过滚动曲线")
        return
    rets, dates_list, mfes, maes = [], [], [], []
    for r in buy_sorted:
        h_data = r.get(f"h_{horizon}")
        if h_data is not None:
            rets.append(h_data["return_pct"])
            mfes.append(h_data["mfe_pct"])
            maes.append(abs(h_data["mae_pct"]))
        else:
            rets.append(None); mfes.append(None); maes.append(None)
        dates_list.append(r["created_at"])

    rolling_wr, rolling_pl, rolling_dates = [], [], []
    for i in range(window - 1, len(rets)):
        seg_ret = [r for r in rets[i - window + 1:i + 1] if r is not None]
        seg_mfe = [m for m in mfes[i - window + 1:i + 1] if m is not None]
        seg_mae = [m for m in maes[i - window + 1:i + 1] if m is not None]
        if not seg_ret:
            continue
        rolling_wr.append(sum(1 for x in seg_ret if x > 0) / len(seg_ret) * 100)
        rolling_pl.append(np.mean(seg_mfe) / np.mean(seg_mae) if seg_mfe and seg_mae and np.mean(seg_mae) > 0 else None)
        rolling_dates.append(dates_list[i])

    fig, ax1 = plt.subplots(figsize=(14, 7))
    ax2 = ax1.twinx()
    ax1.plot(rolling_dates, rolling_wr, color=BLUE, linewidth=2, label=f"滚动胜率 ({window}日)")
    ax1.axhline(50, color="gray", linestyle="--", alpha=0.5, label="50% 基准")
    valid_pl = [(d, p) for d, p in zip(rolling_dates, rolling_pl) if p is not None]
    if valid_pl:
        pl_dates, pl_vals = zip(*valid_pl)
        ax2.plot(pl_dates, pl_vals, color=ORANGE, linewidth=2, linestyle="--", label="盈亏比 (MFE/|MAE|)")
    ax1.set_xlabel("日期")
    ax1.set_ylabel("胜率 (%)", color=BLUE)
    ax2.set_ylabel("盈亏比", color=ORANGE)
    ax1.tick_params(axis="y", labelcolor=BLUE)
    ax2.tick_params(axis="y", labelcolor=ORANGE)
    ax1.set_title(f"滚动胜率 / 盈亏比曲线 (T+{horizon}, {window}日窗口)", fontsize=15, fontweight="bold")
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    fig.autofmt_xdate()
    fig.savefig(CHARTS_DIR / "06_rolling_win_rate.png")
    plt.close(fig)
    print("  ✓ 06 滚动胜率曲线已保存")


def plot_market_state_bars(buy_results: list[dict], bench_name: str = "创业板指", horizon: int = 5):
    """图7：市场状态分层柱状图。"""
    from scipy.stats import ttest_1samp
    states: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        state = r.get("market_state", "unknown")
        states[state].append(h_data["return_pct"])
    if not states:
        return
    order = ["bull", "range", "bear", "unknown"]
    labels, means, errors, significances, n_values = [], [], [], [], []
    for s in order:
        vals = states.get(s, [])
        if not vals:
            continue
        labels.append({"bull": "牛市", "range": "震荡市", "bear": "熊市", "unknown": "未知"}.get(s, s))
        means.append(np.mean(vals))
        errors.append(np.std(vals, ddof=1) / np.sqrt(len(vals)))
        n_values.append(len(vals))
        try:
            _, p = ttest_1samp(vals, 0)
        except Exception:
            p = 1.0
        sig = "**" if p < 0.01 else ("*" if p < 0.05 else "")
        significances.append(sig)

    fig, ax = plt.subplots(figsize=(10, 7))
    bars = ax.bar(labels, means, yerr=errors, capsize=8,
                  color=[GREEN if m > 0 else RED for m in means],
                  alpha=0.7, edgecolor="white", linewidth=1.2)
    for bar, mean, sig, n in zip(bars, means, significances, n_values):
        y_pos = mean + (0.3 if mean >= 0 else -0.8)
        ax.text(bar.get_x() + bar.get_width() / 2, y_pos,
                f"{mean:+.2f}%{sig}\n(n={n})", ha="center", va="center", fontsize=10, fontweight="bold")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel(f"平均 T+{horizon} 收益 (%)")
    ax.set_title(f"市场状态分层收益 (T+{horizon}, vs {bench_name})\n*p<0.05, **p<0.01", fontsize=15, fontweight="bold")
    fig.savefig(CHARTS_DIR / "07_market_state_bars.png")
    plt.close(fig)
    print("  ✓ 07 市场状态分层柱状图已保存")


def plot_confidence_monotonicity(
    buy_results: list[dict], analyses_map: dict[str, dict], horizon: int = 20,
):
    """图8：信号强度分组单调性。"""
    scored = []
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        score = None
        if r["trigger_analysis_id"] and r["trigger_analysis_id"] in analyses_map:
            score = analyses_map[r["trigger_analysis_id"]].get("confidence")
        if score is not None:
            scored.append((score, h_data["return_pct"]))
    if len(scored) < 15:
        print(f"  ⚠ 样本不足 ({len(scored)})，跳过单调性图")
        return

    scored.sort(key=lambda x: x[0])
    n = len(scored)
    n_groups = min(5, n // 3)
    group_size = n // n_groups
    fig, ax = plt.subplots(figsize=(10, 6))
    group_means = []
    group_labels = []
    all_returns = [x[1] for x in scored]
    for g in range(n_groups):
        start = g * group_size
        end = start + group_size if g < n_groups - 1 else n
        rets = [x[1] for x in scored[start:end]]
        group_means.append(np.mean(rets))
        group_labels.append(f"G{g+1}\n(低)" if g == 0 else (f"G{g+1}\n(高)" if g == n_groups - 1 else f"G{g+1}"))
    for i, (label, mean) in enumerate(zip(group_labels, group_means)):
        color = GREEN if mean > 0 else RED
        ax.plot([i, i], [0, mean], color=color, linewidth=3, alpha=0.7)
        ax.scatter([i], [mean], s=200, color=color, zorder=5, edgecolors="white", linewidth=1.5)
        ax.annotate(f"{mean:+.2f}%", (i, mean), textcoords="offset points",
                    xytext=(0, 12 if mean >= 0 else -18), fontsize=11, ha="center", fontweight="bold")
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.axhline(np.mean(all_returns), color=BLUE, linestyle=":", alpha=0.8,
               label=f"全体均值: {np.mean(all_returns):+.2f}%")
    ax.set_xticks(range(len(group_labels)))
    ax.set_xticklabels(group_labels)
    ax.set_ylabel(f"平均 T+{horizon} 收益 (%)")
    ax.set_title(f"信号强度分组单调性测试 (T+{horizon})", fontsize=15, fontweight="bold")
    ax.legend()

    from scipy.stats import spearmanr
    rho, p = spearmanr(range(1, n_groups + 1), group_means)
    conclusion = f"Spearman ρ = {rho:.3f} (p = {p:.3f}) — "
    if rho > 0.3 and p < 0.1:
        conclusion += "正向单调 ✓"
    elif rho < -0.3 and p < 0.1:
        conclusion += "反向单调 ✗"
    else:
        conclusion += "无显著单调性"
    ax.text(0.5, -0.12, conclusion, transform=ax.transAxes, fontsize=11, ha="center", fontstyle="italic")
    fig.savefig(CHARTS_DIR / "08_confidence_monotonicity.png")
    plt.close(fig)
    print("  ✓ 08 单调性测试图已保存")


def plot_drawdown_curve(portfolio_rets: list[float], dates: list[str]):
    """图9：最大回撤曲线。"""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), gridspec_kw={"height_ratios": [2, 1]})

    r = np.array(portfolio_rets, dtype=float)
    cumulative = np.cumprod(1 + r / 100.0)
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]

    ax1.plot(date_objs, cumulative, color=BLUE, linewidth=2, label="组合净值")
    ax1.fill_between(date_objs, 1, cumulative, alpha=0.1, color=BLUE)
    ax1.set_ylabel("净值")
    ax1.set_title("组合净值走势", fontsize=15, fontweight="bold")
    ax1.legend()

    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak * 100
    ax2.fill_between(date_objs, drawdown, 0, color=RED, alpha=0.3)
    ax2.plot(date_objs, drawdown, color=RED, linewidth=1.5)
    ax2.axhline(0, color="black", linewidth=0.5)
    mdd = np.min(drawdown)
    ax2.axhline(mdd, color="darkred", linestyle="--", linewidth=1, alpha=0.7, label=f"最大回撤: {mdd:.2f}%")
    ax2.set_xlabel("日期")
    ax2.set_ylabel("回撤 (%)")
    ax2.set_title("回撤走势", fontsize=15, fontweight="bold")
    ax2.legend()

    ax1.xaxis.set_major_formatter(DateFormatter("%m-%d"))
    ax2.xaxis.set_major_formatter(DateFormatter("%m-%d"))

    fig.autofmt_xdate()
    fig.savefig(CHARTS_DIR / "09_drawdown_curve.png")
    plt.close(fig)
    print("  ✓ 09 最大回撤曲线已保存")


def plot_monthly_calendar(buy_results: list[dict], horizon: int = 20):
    """图10：月度收益日历（按月的平均收益）。"""
    # 按月份汇总所有 buy 的 T+N 收益
    monthly: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        month_key = r["created_at"].strftime("%Y-%m")
        monthly[month_key].append(h_data["return_pct"])

    if not monthly:
        return

    months = sorted(monthly.keys())
    means = [np.mean(monthly[m]) for m in months]
    win_rates = [sum(1 for x in monthly[m] if x > 0) / len(monthly[m]) * 100 for m in months]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))

    colors_bar = [GREEN if m > 0 else RED for m in means]
    ax1.bar(months, means, color=colors_bar, alpha=0.7, edgecolor="white")
    for i, (m, v, n) in enumerate(zip(months, means, [len(monthly[m]) for m in months])):
        ax1.text(i, v + (0.3 if v >= 0 else -0.8), f"{v:+.2f}%\n(n={n})",
                 ha="center", fontsize=9, fontweight="bold")
    ax1.axhline(0, color="black", linewidth=0.8)
    ax1.set_title(f"月度平均 T+{horizon} 收益", fontsize=15, fontweight="bold")
    ax1.set_ylabel("平均收益 (%)")

    colors_wr = [GREEN if w > 50 else RED for w in win_rates]
    ax2.bar(months, win_rates, color=colors_wr, alpha=0.7, edgecolor="white")
    ax2.axhline(50, color="gray", linestyle="--", alpha=0.7, label="50%")
    for i, (m, w) in enumerate(zip(months, win_rates)):
        ax2.text(i, w + 0.5, f"{w:.0f}%", ha="center", fontsize=9)
    ax2.set_title(f"月度胜率 (T+{horizon})", fontsize=15, fontweight="bold")
    ax2.set_ylabel("胜率 (%)")
    ax2.legend()

    fig.tight_layout()
    fig.savefig(CHARTS_DIR / "10_monthly_calendar.png")
    plt.close(fig)
    print("  ✓ 10 月度收益日历已保存")


def plot_risk_level_boxplot(buy_results: list[dict], horizon: int = 20):
    """图11：风险等级分层箱线图。"""
    by_risk: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        risk = r.get("risk_level") or "unknown"
        by_risk[risk].append(h_data["return_pct"])

    if not by_risk:
        return

    risk_order = [k for k in ["low", "medium", "high", "critical", "unknown"] if k in by_risk]
    data_groups = [by_risk[k] for k in risk_order]
    labels = [f"{k}\n(n={len(by_risk[k])})" for k in risk_order]

    fig, ax = plt.subplots(figsize=(10, 7))
    bp = ax.boxplot(data_groups, widths=0.5, patch_artist=True,
                    medianprops={"color": "black", "linewidth": 2},
                    flierprops={"marker": "o", "markerfacecolor": RED, "alpha": 0.5})
    box_colors = [GREEN, BLUE, ORANGE, RED, GRAY][:len(data_groups)]
    for patch, color in zip(bp["boxes"], box_colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.3)

    for i, data in enumerate(data_groups):
        jitter = np.random.normal(0, 0.04, len(data))
        ax.scatter(np.ones(len(data)) * (i + 1) + jitter, data, alpha=0.2, s=12, color="black")

    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.set_xticklabels(labels)
    ax.set_ylabel(f"T+{horizon} 收益 (%)")
    ax.set_title(f"风险等级分层收益 (T+{horizon})", fontsize=15, fontweight="bold")

    # 额外统计标注
    stats_text = ""
    for k, data in zip(risk_order, data_groups):
        st = _stats(data)
        stats_text += f"{k}: 均值 {st['mean']:+.2f}%, 胜率 {sum(1 for x in data if x>0)/len(data)*100:.0f}%\n"
    ax.text(0.98, 0.05, stats_text, transform=ax.transAxes, fontsize=9,
            ha="right", va="bottom", bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.8})

    fig.savefig(CHARTS_DIR / "11_risk_level_boxplot.png")
    plt.close(fig)
    print("  ✓ 11 风险等级分层箱线图已保存")


def plot_sector_returns(buy_results: list[dict], stock_concepts: dict[str, list[str]], top_n: int = 15):
    """图12：行业/概念收益排名。"""
    concept_rets: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        sym = r["symbol"]
        h20 = r.get("h_20")
        if h20 is None:
            continue
        concepts = stock_concepts.get(sym, [])
        for c in concepts:
            concept_rets[c].append(h20["return_pct"])

    if not concept_rets:
        print("  ⚠ 无板块数据，跳过板块收益图")
        return

    # 取样本数量 >= 3 的概念
    valid_concepts = [(c, rets) for c, rets in concept_rets.items() if len(rets) >= 3]
    valid_concepts.sort(key=lambda x: np.mean(x[1]), reverse=True)

    top = valid_concepts[:top_n]
    bottom = valid_concepts[-top_n:] if len(valid_concepts) > top_n else []
    display = top + bottom

    if not display:
        return

    names = []
    means_list = []
    counts = []
    colors_bar = []
    for c, rets in display:
        # 缩短概念名
        short_name = c if len(c) <= 12 else c[:11] + "…"
        names.append(short_name)
        m = np.mean(rets)
        means_list.append(m)
        counts.append(len(rets))
        colors_bar.append(GREEN if m > 0 else RED)

    fig, ax = plt.subplots(figsize=(12, max(8, len(display) * 0.4)))
    y_pos = range(len(display))
    ax.barh(y_pos, means_list, color=colors_bar, alpha=0.7, edgecolor="white")
    ax.axvline(0, color="black", linewidth=0.8)
    for i, (m, c) in enumerate(zip(means_list, counts)):
        ax.text(m + (0.3 if m >= 0 else -0.8), i, f"{m:+.2f}% (n={c})", va="center", fontsize=8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=9)
    ax.set_xlabel("平均 T+20 收益 (%)")
    ax.set_title("行业/概念收益排名 (T+20, 样本≥3)", fontsize=15, fontweight="bold")
    ax.invert_yaxis()

    fig.savefig(CHARTS_DIR / "12_sector_returns.png")
    plt.close(fig)
    print("  ✓ 12 行业/概念收益排名已保存")


# ════════════════════════════════════════════════════════════════════════
# Markdown 报告生成
# ════════════════════════════════════════════════════════════════════════

def generate_markdown_report(
    ops: list[dict], analyses_map: dict[str, dict], feedbacks: list[dict],
    buy_results: list[dict], skip_results: list[dict], sell_results: list[dict],
    portfolio_rets: list[float], portfolio_dates: list[str],
    ic_seq: dict[int, dict], stock_concepts: dict[str, list[str]],
):
    buys = [o for o in buy_results]
    buys_approved = [r for r in buy_results if r["status"] == "approved"]
    buys_rejected = [r for r in buy_results if r["status"] == "rejected"]
    buys_pending = [r for r in buy_results if r["status"] == "pending"]
    buys_triggered = [r for r in buy_results if r["status"] == "triggered_close"]

    lines = []
    def w(*args):
        lines.append("".join(str(a) for a in args))

    w("# 回测评估报告")
    w()
    w(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    w(f"**数据来源**: quant_kb.trading_operations + C:/klines/daily/")
    w(f"**回测区间**: {min(o['created_at'] for o in ops).strftime('%Y-%m-%d')} ~ {max(o['created_at'] for o in ops).strftime('%Y-%m-%d')}")
    w()

    # ── 总览 ──
    w("---")
    w()
    w("## 0. 数据总览")
    w()
    w(f"| 指标 | 数值 |")
    w(f"|------|------|")
    w(f"| 总交易操作 | {len(ops)} |")
    w(f"| Buy (有效) | {len(buys)} |")
    w(f"|  - approved | {len(buys_approved)} |")
    w(f"|  - rejected | {len(buys_rejected)} |")
    w(f"|  - pending | {len(buys_pending)} |")
    w(f"|  - triggered_close | {len(buys_triggered)} |")
    w(f"| Skip (有效) | {len(skip_results)} |")
    w(f"| Sell (有效) | {len(sell_results)} |")
    w(f"| 涉及标的数 | {len(set(r['symbol'] for r in buy_results))} |")
    w(f"| 有 analysis 关联 | {sum(1 for r in buy_results if r['trigger_analysis_id'])}/({len(buys)}) |")
    w()

    # ── A. 组合层面指标 ──
    w("---")
    w()
    w("## A. 组合层面指标")
    w()
    if portfolio_rets:
        total_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] - 1) * 100
        ann_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] ** (252 / len(portfolio_rets)) - 1) * 100
        ann_vol = float(np.std(portfolio_rets, ddof=1) * np.sqrt(252))
        sr = sharpe_ratio(portfolio_rets)
        sor = sortino_ratio(portfolio_rets)
        mdd_info = max_drawdown_details(portfolio_rets)
        cal = calmar_ratio(portfolio_rets, mdd_info["mdd_pct"])
        win_rate = sum(1 for x in portfolio_rets if x > 0) / len(portfolio_rets) * 100
        pos_days = sum(1 for x in portfolio_rets if x > 0)
        neg_days = sum(1 for x in portfolio_rets if x < 0)

        w(f"| 指标 | 数值 |")
        w(f"|------|------|")
        w(f"| 总收益 | {total_ret:+.2f}% |")
        w(f"| 年化收益率 | {ann_ret:+.2f}% |")
        w(f"| 年化波动率 | {ann_vol:.2f}% |")
        w(f"| Sharpe Ratio | {sr:.3f} |")
        w(f"| Sortino Ratio | {sor:.3f} |")
        w(f"| Calmar Ratio | {cal:.3f} |")
        w(f"| 最大回撤 | {mdd_info['mdd_pct']:.2f}% |")
        w(f"| 回撤持续时间 | {mdd_info['duration_days']} 天 |")
        w(f"| 恢复天数 | {mdd_info['recovery_days']} 天 |")
        w(f"| 日胜率 | {win_rate:.1f}% ({pos_days}/{pos_days + neg_days}) |")

        # 与基准对比
        w()
        w("### 与基准指数对比")
        w()
        w(f"| 基准 | 总收益 | 年化收益 | 年化波动 | Sharpe | 最大回撤 |")
        w(f"|------|--------|----------|----------|--------|----------|")
        for name, _ in BENCHMARKS:
            b_rets = bench_rets.get(name, [])
            if not b_rets:
                continue
            b_total = (np.cumprod(1 + np.array(b_rets) / 100.0)[-1] - 1) * 100
            b_ann = (np.cumprod(1 + np.array(b_rets) / 100.0)[-1] ** (252 / len(b_rets)) - 1) * 100
            b_vol = float(np.std(b_rets, ddof=1) * np.sqrt(252))
            b_sr = sharpe_ratio(b_rets)
            b_mdd = max_drawdown_details(b_rets)["mdd_pct"]
            w(f"| {name} | {b_total:+.2f}% | {b_ann:+.2f}% | {b_vol:.2f}% | {b_sr:.3f} | {b_mdd:.2f}% |")
        w()
        w(f"![组合净值](charts/01_portfolio_equity.png)")
        w(f"![回撤曲线](charts/09_drawdown_curve.png)")

    w()

    # ── B. 交易信号分析 ──
    w("---")
    w()
    w("## B. 交易信号分析")
    w()
    for h in HORIZONS:
        rets = [r[f"h_{h}"]["return_pct"] for r in buy_results if r.get(f"h_{h}") is not None]
        if not rets:
            continue
        st = _stats(rets)
        mm = compute_mfe_mae_ratio(buy_results, h)
        wr = sum(1 for x in rets if x > 0) / len(rets) * 100
        w(f"### T+{h} 个交易日 (N={len(rets)})")
        w(f"| 指标 | 数值 |")
        w(f"|------|------|")
        w(f"| 平均收益 | {st['mean']:+.2f}% |")
        w(f"| 中位数收益 | {st['median']:+.2f}% |")
        w(f"| 标准差 | {st['std']:.2f}% |")
        w(f"| 最佳 | {st['max']:+.2f}% |")
        w(f"| 最差 | {st['min']:+.2f}% |")
        w(f"| 胜率 | {wr:.1f}% |")
        w(f"| MFE 均值 | {mm['mfe']['mean']:+.2f}% |")
        w(f"| MAE 均值 | {mm['mae']['mean']:+.2f}% |")
        w(f"| MFE/MAE 比率 | {mm['ratio_mean']:.2f} |")
        w()

    # 超额收益
    w("### 超额收益 vs 基准")
    w()
    header = "| 持有期 |"
    for name, _ in BENCHMARKS:
        header += f" vs {name} |"
    w(header)
    w(f"|--------|" + "|".join(["---------"] * len(BENCHMARKS)) + "|")
    for h in HORIZONS:
        row = f"| T+{h} |"
        for name, _ in BENCHMARKS:
            b_data = benchmark_data.get(name, {})
            b_dates_list = benchmark_dates.get(name, [])
            excess = compute_excess_returns(buy_results, b_data, b_dates_list, h)
            if excess:
                ex = _stats(excess)
                row += f" {ex['mean']:+.2f}% (胜率 {sum(1 for x in excess if x>0)/len(excess)*100:.0f}%) |"
            else:
                row += f" - |"
        w(row)
    w()

    # IC 分析
    w("### 信息系数 (IC) 分析")
    w()
    w(f"| 持有期 | Rank IC | N | 评估 |")
    w(f"|--------|---------|---|------|")
    for h in HORIZONS:
        info = ic_seq.get(h, {})
        ic = info.get("ic")
        if ic is not None:
            assessment = "有效 ✓" if abs(ic) > 0.05 else "微弱"
            w(f"| T+{h} | {ic:+.4f} | {info['n']} | {assessment} |")
        else:
            w(f"| T+{h} | - | {info.get('n', 0)} | 数据不足 |")
    w()
    w(f"![IC衰减](charts/05_ic_decay.png)")
    w()
    w(f"![收益热力图](charts/02_return_heatmap.png)")
    w(f"![MFE/MAE散点图](charts/03_mfe_mae_scatter.png)")
    w(f"![收益箱线图](charts/04_return_boxplots.png)")
    w()

    # ── C. 风控诊断 ──
    w("---")
    w()
    w("## C. 风控诊断")
    w()
    w("### 按风险等级分层 (T+20)")
    w()
    by_risk = defaultdict(list)
    for r in buy_results:
        h20 = r.get("h_20")
        if h20 is None:
            continue
        risk = r.get("risk_level") or "unknown"
        by_risk[risk].append(h20["return_pct"])
    w(f"| 风险等级 | N | 平均收益 | 中位数 | 胜率 | 最大回撤均值 |")
    w(f"|----------|---|----------|--------|------|--------------|")
    for risk in ["low", "medium", "high", "critical", "unknown"]:
        vals = by_risk.get(risk, [])
        if vals:
            st = _stats(vals)
            avg_mae = np.mean([abs(r.get(f"h_20", {}).get("mae_pct", 0)) for r in buy_results
                               if (r.get("risk_level") or "unknown") == risk and r.get("h_20")])
            w(f"| {risk} | {len(vals)} | {st['mean']:+.2f}% | {st['median']:+.2f}% | {sum(1 for x in vals if x>0)/len(vals)*100:.1f}% | {avg_mae:.2f}% |")
    w()
    w(f"![风险等级箱线图](charts/11_risk_level_boxplot.png)")
    w()

    # 连续亏损
    if portfolio_rets:
        cl = compute_consecutive_losses(portfolio_rets)
        w("### 连续亏损统计")
        w(f"| 指标 | 数值 |")
        w(f"|------|------|")
        w(f"| 最大连续亏损天数 | {cl['max_consecutive_losses']} 天 |")
        w(f"| 平均连续亏损天数 | {cl['avg_consecutive_losses']:.1f} 天 |")
    w()

    # Stop Loss / Take Profit
    stop_loss_ops = [o for o in ops if o["operation_type"] == "stop_loss"]
    take_profit_ops = [o for o in ops if o["operation_type"] == "take_profit"]
    if stop_loss_ops or take_profit_ops:
        w("### 止损/止盈统计")
        w(f"| 类型 | 次数 |")
        w(f"|------|------|")
        w(f"| Stop Loss | {len(stop_loss_ops)} |")
        w(f"| Take Profit | {len(take_profit_ops)} |")
    w()

    # ── D. 择时/市场环境 ──
    w("---")
    w()
    w("## D. 择时/市场环境")
    w()
    w("### 市场状态分层 (T+20)")
    w()
    states = defaultdict(list)
    for r in buy_results:
        h20 = r.get("h_20")
        if h20 is not None:
            states[r.get("market_state", "unknown")].append(h20["return_pct"])
    w(f"| 市场状态 | N | 平均收益 | 中位数 | 胜率 |")
    w(f"|----------|---|----------|--------|------|")
    for s in ["bull", "range", "bear", "unknown"]:
        vals = states.get(s, [])
        if vals:
            st = _stats(vals)
            w(f"| {s} | {len(vals)} | {st['mean']:+.2f}% | {st['median']:+.2f}% | {sum(1 for x in vals if x>0)/len(vals)*100:.1f}% |")
    w()
    w(f"![市场状态分层](charts/07_market_state_bars.png)")
    w(f"![滚动胜率](charts/06_rolling_win_rate.png)")
    w(f"![月度收益日历](charts/10_monthly_calendar.png)")
    w()

    # ── E. 行业/板块暴露 ──
    w("---")
    w()
    w("## E. 行业/板块暴露")
    w()
    if stock_concepts:
        concept_counts: dict[str, int] = defaultdict(int)
        concept_rets: dict[str, list[float]] = defaultdict(list)
        for r in buy_results:
            sym = r["symbol"]
            concepts = stock_concepts.get(sym, [])
            h20 = r.get("h_20")
            for c in concepts:
                concept_counts[c] += 1
                if h20 is not None:
                    concept_rets[c].append(h20["return_pct"])

        w("### 最频繁交易的板块 (Top 10)")
        w()
        top_concepts = sorted(concept_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        w(f"| 板块 | 交易次数 | 平均T+20收益 |")
        w(f"|------|----------|-------------|")
        for c, cnt in top_concepts:
            rets = concept_rets.get(c, [])
            avg = f"{np.mean(rets):+.2f}%" if rets else "-"
            w(f"| {c} | {cnt} | {avg} |")
        w()
        w(f"![板块收益排名](charts/12_sector_returns.png)")
    w()

    # ── F. 决策流程诊断 ──
    w("---")
    w()
    w("## F. 决策流程诊断")
    w()

    # approved vs rejected
    w("### Buy Approved vs Rejected (T+30)")
    w()
    w(f"| 状态 | N | T+30平均收益 | MFE均值 | |MAE|均值 | MFE/MAE |")
    w(f"|------|---|--------------|---------|-----------|---------|")
    for label in ["approved", "rejected", "pending", "triggered_close"]:
        group = [r for r in buy_results if r["status"] == label]
        rets = [r["h_30"]["return_pct"] for r in group if r.get("h_30") is not None]
        if rets:
            mm30 = compute_mfe_mae_ratio(group, 30)
            w(f"| {label} | {len(rets)} | {np.mean(rets):+.2f}% | {mm30['mfe']['mean']:+.2f}% | {mm30['mae']['mean']:+.2f}% | {mm30['ratio_mean']:.2f} |")
    w()
    w(f"![单调性测试](charts/08_confidence_monotonicity.png)")
    w()

    # Skip 决策质量
    w("### Skip 决策质量")
    w()
    if skip_results:
        for h in [5, 20]:
            s_rets = [r[f"h_{h}"]["return_pct"] for r in skip_results if r.get(f"h_{h}") is not None]
            if not s_rets:
                continue
            missed_up5 = sum(1 for x in s_rets if x > 5)
            missed_up10 = sum(1 for x in s_rets if x > 10)
            avoided5 = sum(1 for x in s_rets if x < -5)
            avoided10 = sum(1 for x in s_rets if x < -10)
            w(f"**T+{h} (N={len(s_rets)})**")
            w(f"| 指标 | 数值 |")
            w(f"|------|------|")
            w(f"| 正确跳过(跌>5%) | {avoided5} ({avoided5/len(s_rets)*100:.1f}%) |")
            w(f"| 正确跳过(跌>10%) | {avoided10} ({avoided10/len(s_rets)*100:.1f}%) |")
            w(f"| 错过涨幅(涨>5%) | {missed_up5} ({missed_up5/len(s_rets)*100:.1f}%) |")
            w(f"| 错过涨幅(涨>10%) | {missed_up10} ({missed_up10/len(s_rets)*100:.1f}%) |")
            w()

    # Sell 时机分析
    if sell_results:
        w("### Sell 时机分析")
        w()
        for h in HORIZONS:
            sell_rets = [r[f"h_{h}"]["return_pct"] for r in sell_results if r.get(f"h_{h}")]
            if not sell_rets:
                continue
            st = _stats(sell_rets)
            after_sell_up = sum(1 for x in sell_rets if x > 5)
            after_sell_down = sum(1 for x in sell_rets if x < -5)
            w(f"**T+{h} (N={len(sell_rets)})**: 卖出后平均 {st['mean']:+.2f}%, "
              f"继续涨>5%: {after_sell_up}次, 继续跌>5%: {after_sell_down}次")

    w()
    w("---")
    w()
    w("## 图表索引")
    w()
    for i, desc in enumerate([
        "组合净值 vs 基准", "T+N 收益热力图", "MFE/MAE 散点图",
        "T+1/5/20 收益箱线图", "IC 衰减曲线", "滚动胜率/盈亏比",
        "市场状态分层柱状图", "信号强度单调性", "最大回撤曲线",
        "月度收益日历", "风险等级分层箱线图", "行业概念收益排名",
    ], 1):
        w(f"{i}. {desc}")
    w()
    w(f"*报告生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  Markdown 报告已保存: {REPORT_PATH}")


# ════════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════════

# 全局变量（跨函数共享，避免重复加载）
benchmark_data: dict[str, dict[str, dict]] = {}
benchmark_dates: dict[str, list[str]] = {}
bench_rets: dict[str, list[float]] = {}
bench_dts: dict[str, list[str]] = {}


def analyze(ops: list[dict], analyses_map: dict[str, dict], feedbacks: list[dict]):
    global benchmark_data, benchmark_dates, bench_rets, bench_dts

    # ── 数据预处理 ──
    klines_cache: dict[str, dict[str, dict]] = {}
    dates_cache: dict[str, list[str]] = {}

    def _ensure_klines(sym: str):
        if sym not in klines_cache:
            klines_cache[sym] = load_klines(sym)
            dates_cache[sym] = _sorted_dates(klines_cache[sym])

    buys = [o for o in ops if o["operation_type"] == "buy" and o["symbol"]]
    sells = [o for o in ops if o["operation_type"] == "sell" and o["symbol"]]
    skips = [o for o in ops if o["operation_type"] == "skip" and o["symbol"]]
    stop_loss_ops = [o for o in ops if o["operation_type"] == "stop_loss"]
    take_profit_ops = [o for o in ops if o["operation_type"] == "take_profit"]

    symbols_needed = set(o["symbol"] for o in buys + skips + sells)
    print(f"加载日线数据 {len(symbols_needed)} 个标的...")
    for i, sym in enumerate(sorted(symbols_needed)):
        _ensure_klines(sym)
        if (i + 1) % 50 == 0 or i + 1 == len(symbols_needed):
            print(f"  {i + 1}/{len(symbols_needed)}")

    def _has_enough_data(o) -> bool:
        dates = dates_cache.get(o["symbol"], [])
        if not dates:
            return False
        effective_date = _effective_buy_date(o["created_at"])
        _, _, idx = get_close_on_or_after(dates, klines_cache[o["symbol"]], effective_date)
        if idx is None:
            return False
        return len(dates) - idx > max(HORIZONS)

    buys_filtered = [o for o in buys if _has_enough_data(o)]
    skips_filtered = [o for o in skips if _has_enough_data(o)]
    sells_filtered = [o for o in sells if _has_enough_data(o)]
    if len(buys_filtered) < len(buys):
        print(f"  过滤掉 {len(buys) - len(buys_filtered)} 条 buy（数据不足）")
    if len(skips_filtered) < len(skips):
        print(f"  过滤掉 {len(skips) - len(skips_filtered)} 条 skip（数据不足）")

    print(f"\n总记录: {len(ops)}")
    print(f"  buy : {len(buys_filtered)} (原始 {len(buys)})")
    print(f"  sell: {len(sells_filtered)} (原始 {len(sells)})")
    print(f"  skip: {len(skips_filtered)} (原始 {len(skips)})")
    print(f"  stop_loss: {len(stop_loss_ops)}")
    print(f"  take_profit: {len(take_profit_ops)}")

    # ── 加载基准指数 ──
    print(f"\n加载基准指数: {[n for n, _ in BENCHMARKS]}")
    for name, _ in BENCHMARKS:
        benchmark_data[name] = load_klines(name)
        benchmark_dates[name] = _sorted_dates(benchmark_data[name])
        print(f"  {name}: {len(benchmark_data[name])} 个交易日")

    # ── 加载板块数据 ──
    print("\n加载板块映射...")
    stock_concepts = load_stock_concepts()
    print(f"  板块映射: {len(stock_concepts)} 只股票")

    # ── 对所有 buy 计算完整指标 ──
    print("\n计算买入后收益指标 (含 MFE/MAE)...")
    buy_results: list[dict] = []
    for o in buys_filtered:
        klines = klines_cache[o["symbol"]]
        effective_date = _effective_buy_date(o["created_at"])
        dates = dates_cache[o["symbol"]]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue
        rets = get_next_n_returns(dates, klines, idx, close)
        cy_klines = benchmark_data.get("创业板指", {})
        cy_dates = benchmark_dates.get("创业板指", [])
        market_state = classify_market_state(cy_klines, cy_dates, found_date) if cy_klines else "unknown"
        entry = {
            "symbol": o["symbol"], "created_at": o["created_at"],
            "status": o["status"], "risk_level": o.get("risk_level") or "unknown",
            "trigger_analysis_id": o.get("trigger_analysis_id"),
            "buy_price": close, "buy_date": found_date,
            "effective_buy_date": effective_date, "market_state": market_state,
            "price": o.get("price"), "quantity": o.get("quantity"),
        }
        for h in HORIZONS:
            entry[f"h_{h}"] = rets.get(h)
        buy_results.append(entry)
    print(f"  有效买入: {len(buy_results)} 条")

    # ── Skip 计算 ──
    skip_results: list[dict] = []
    for o in skips_filtered:
        klines = klines_cache[o["symbol"]]
        effective_date = _effective_buy_date(o["created_at"])
        dates = dates_cache[o["symbol"]]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue
        rets = get_next_n_returns(dates, klines, idx, close)
        entry = {"symbol": o["symbol"], "created_at": o["created_at"],
                 "buy_price": close, "buy_date": found_date}
        for h in HORIZONS:
            entry[f"h_{h}"] = rets.get(h)
        skip_results.append(entry)
    print(f"  有效 Skip: {len(skip_results)} 条")

    # ── Sell 计算 ──
    sell_results: list[dict] = []
    for o in sells_filtered:
        klines = klines_cache[o["symbol"]]
        effective_date = _effective_buy_date(o["created_at"])
        dates = dates_cache[o["symbol"]]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue
        rets = get_next_n_returns(dates, klines, idx, close)
        entry = {"symbol": o["symbol"], "created_at": o["created_at"],
                 "buy_price": close, "buy_date": found_date}
        for h in HORIZONS:
            entry[f"h_{h}"] = rets.get(h)
        sell_results.append(entry)
    print(f"  有效 Sell: {len(sell_results)} 条")

    # ── 组合层面计算 ──
    print("\n计算组合日收益...")
    portfolio_rets, portfolio_dates, bench_rets, bench_dts = compute_portfolio_daily_returns(
        buy_results, klines_cache, dates_cache)
    print(f"  组合交易日: {len(portfolio_rets)}")

    # ── IC 分析 ──
    print("\n计算 IC 序列...")
    ic_seq = compute_ic_sequence(buy_results, analyses_map)

    # ═══════════════════════════════════════════
    # 终端报告输出
    # ═══════════════════════════════════════════

    # A. 组合层面指标
    print("\n" + "=" * 80)
    print("A. 组合层面指标")
    print("=" * 80)
    if portfolio_rets:
        total_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] - 1) * 100
        ann_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] ** (252 / len(portfolio_rets)) - 1) * 100
        ann_vol = float(np.std(portfolio_rets, ddof=1) * np.sqrt(252))
        sr = sharpe_ratio(portfolio_rets)
        sor = sortino_ratio(portfolio_rets)
        mdd_info = max_drawdown_details(portfolio_rets)
        cal = calmar_ratio(portfolio_rets, mdd_info["mdd_pct"])
        print(f"  总收益: {total_ret:+.2f}%")
        print(f"  年化收益率: {ann_ret:+.2f}%")
        print(f"  年化波动率: {ann_vol:.2f}%")
        print(f"  Sharpe Ratio: {sr:.3f}")
        print(f"  Sortino Ratio: {sor:.3f}")
        print(f"  Calmar Ratio: {cal:.3f}")
        print(f"  最大回撤: {mdd_info['mdd_pct']:.2f}% (持续 {mdd_info['duration_days']} 天)")
        print(f"  日胜率: {sum(1 for x in portfolio_rets if x > 0)/len(portfolio_rets)*100:.1f}%")

        cl = compute_consecutive_losses(portfolio_rets)
        print(f"  最大连续亏损天数: {cl['max_consecutive_losses']}")

        print("\n  与基准指数对比:")
        print(f"  {'基准':<10s} {'总收益':>8s} {'年化收益':>8s} {'年化波动':>8s} {'Sharpe':>7s} {'MDD':>7s}")
        for name, _ in BENCHMARKS:
            b_rets = bench_rets.get(name, [])
            if not b_rets:
                continue
            b_total = (np.cumprod(1 + np.array(b_rets) / 100.0)[-1] - 1) * 100
            b_ann = (np.cumprod(1 + np.array(b_rets) / 100.0)[-1] ** (252 / len(b_rets)) - 1) * 100
            b_vol = float(np.std(b_rets, ddof=1) * np.sqrt(252))
            b_sr = sharpe_ratio(b_rets)
            b_mdd = max_drawdown_details(b_rets)["mdd_pct"]
            print(f"  {name:<10s} {b_total:+7.2f}% {b_ann:+7.2f}% {b_vol:+7.2f}% {b_sr:+6.3f} {b_mdd:+6.2f}%")

    # B. 交易信号分析
    print("\n" + "=" * 80)
    print("B. 交易信号分析")
    print("=" * 80)
    for h in HORIZONS:
        rets = [r[f"h_{h}"]["return_pct"] for r in buy_results if r.get(f"h_{h}") is not None]
        if not rets:
            continue
        st = _stats(rets)
        mm = compute_mfe_mae_ratio(buy_results, h)
        wr = sum(1 for x in rets if x > 0) / len(rets) * 100
        print(f"\n  T+{h} (N={len(rets)}): 均值 {st['mean']:+.2f}%  中位数 {st['median']:+.2f}%  "
              f"胜率 {wr:.1f}%  MFE均值 {mm['mfe']['mean']:+.2f}%  MAE均值 {mm['mae']['mean']:+.2f}%  "
              f"MFE/MAE比率 {mm['ratio_mean']:.2f}")

    # IC
    print("\n  信息系数 (IC) 分析:")
    for h in HORIZONS:
        info = ic_seq.get(h, {})
        ic = info.get("ic")
        if ic is not None:
            print(f"    T+{h}: Rank IC = {ic:+.4f} (N={info['n']}) {'✓ 有效' if abs(ic) > 0.05 else '✗ 微弱'}")
        else:
            print(f"    T+{h}: 数据不足")

    # C. 风控诊断
    print("\n" + "=" * 80)
    print("C. 风控诊断")
    print("=" * 80)
    by_risk = defaultdict(list)
    for r in buy_results:
        h20 = r.get("h_20")
        if h20 is None:
            continue
        risk = r.get("risk_level") or "unknown"
        by_risk[risk].append(h20["return_pct"])
    for risk in ["low", "medium", "high", "critical", "unknown"]:
        vals = by_risk.get(risk, [])
        if vals:
            st = _stats(vals)
            print(f"  {risk:>10s} (N={len(vals):3d}): T+20 均值 {st['mean']:+.2f}%  胜率 {sum(1 for x in vals if x>0)/len(vals)*100:.1f}%")

    if stop_loss_ops or take_profit_ops:
        print(f"  Stop Loss: {len(stop_loss_ops)} 次, Take Profit: {len(take_profit_ops)} 次")

    # D. 择时/市场环境
    print("\n" + "=" * 80)
    print("D. 择时/市场环境")
    print("=" * 80)
    states = defaultdict(list)
    for r in buy_results:
        h20 = r.get("h_20")
        if h20 is not None:
            states[r.get("market_state", "unknown")].append(h20["return_pct"])
    for s in ["bull", "range", "bear", "unknown"]:
        vals = states.get(s, [])
        if vals:
            st = _stats(vals)
            print(f"  {s:>8s} (N={len(vals):3d}): T+20 均值 {st['mean']:+.2f}%  "
                  f"中位数 {st['median']:+.2f}%  胜率 {sum(1 for x in vals if x>0)/len(vals)*100:.1f}%")

    # E. 行业/板块暴露
    print("\n" + "=" * 80)
    print("E. 行业/板块暴露 (最频繁交易的前10板块)")
    print("=" * 80)
    if stock_concepts:
        concept_counts = defaultdict(int)
        concept_rets = defaultdict(list)
        for r in buy_results:
            sym = r["symbol"]
            h20 = r.get("h_20")
            for c in stock_concepts.get(sym, []):
                concept_counts[c] += 1
                if h20 is not None:
                    concept_rets[c].append(h20["return_pct"])
        top = sorted(concept_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        for c, cnt in top:
            rets = concept_rets.get(c, [])
            avg = f"{np.mean(rets):+.2f}%" if rets else "-"
            print(f"  {c:<20s}: {cnt:3d}次, T+20 平均收益 {avg}")

    # F. 决策流程诊断
    print("\n" + "=" * 80)
    print("F. 决策流程诊断")
    print("=" * 80)
    for label in ["approved", "rejected", "pending", "triggered_close"]:
        group = [r for r in buy_results if r["status"] == label]
        rets30 = [r["h_30"]["return_pct"] for r in group if r.get("h_30") is not None]
        if rets30:
            mm30 = compute_mfe_mae_ratio(group, 30)
            print(f"  {label:<20s} (N={len(rets30):3d}): T+30 均值 {np.mean(rets30):+.2f}%  "
                  f"MFE/MAE {mm30['ratio_mean']:.2f}")

    print("\n  Skip 决策质量 (T+20):")
    if skip_results:
        s_rets20 = [r["h_20"]["return_pct"] for r in skip_results if r.get("h_20")]
        if s_rets20:
            missed = sum(1 for x in s_rets20 if x > 5)
            avoided = sum(1 for x in s_rets20 if x < -5)
            print(f"    正确跳过(跌>5%): {avoided}次 ({avoided/len(s_rets20)*100:.1f}%)")
            print(f"    错过涨幅(涨>5%): {missed}次 ({missed/len(s_rets20)*100:.1f}%)")

    if sell_results:
        print(f"\n  Sell 卖出后表现 (T+5/20):")
        for h in [5, 20]:
            s_rets = [r[f"h_{h}"]["return_pct"] for r in sell_results if r.get(f"h_{h}")]
            if s_rets:
                st = _stats(s_rets)
                print(f"    T+{h}: 均值 {st['mean']:+.2f}%  "
                      f"(跌>5%: {sum(1 for x in s_rets if x<-5)}次, 涨>5%: {sum(1 for x in s_rets if x>5)}次)")

    # ═══════════════════════════════════════════
    # 生成可视化图表
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("生成可视化图表...")
    print("=" * 80)

    if portfolio_rets and portfolio_dates:
        plot_portfolio_equity(portfolio_rets, portfolio_dates, bench_rets, bench_dts)
    plot_return_heatmap(buy_results)
    plot_mfe_mae_scatter(buy_results, horizon=20)
    plot_return_boxplots(buy_results)
    plot_ic_decay(ic_seq)
    if len(buy_results) >= 60:
        plot_rolling_win_rate(buy_results, window=min(60, len(buy_results)), horizon=20)
    else:
        plot_rolling_win_rate(buy_results, window=len(buy_results)//2, horizon=20)
    plot_market_state_bars(buy_results, "创业板指", horizon=5)
    plot_confidence_monotonicity(buy_results, analyses_map, horizon=20)
    if portfolio_rets:
        plot_drawdown_curve(portfolio_rets, portfolio_dates)
    plot_monthly_calendar(buy_results, horizon=20)
    plot_risk_level_boxplot(buy_results, horizon=20)
    plot_sector_returns(buy_results, stock_concepts, top_n=15)

    # ═══════════════════════════════════════════
    # 生成 Markdown 报告
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("生成 Markdown 报告...")
    print("=" * 80)
    generate_markdown_report(
        ops, analyses_map, feedbacks,
        buy_results, skip_results, sell_results,
        portfolio_rets, portfolio_dates,
        ic_seq, stock_concepts,
    )

    print(f"\n{'=' * 80}")
    print(f"报告生成完成！")
    print(f"  报告文件: {REPORT_PATH}")
    print(f"  图表目录: {CHARTS_DIR.resolve()}")
    print(f"{'=' * 80}")


def main():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, dbname=DB_NAME,
    )
    conn.set_client_encoding("UTF8")
    try:
        ops = fetch_operations(conn)
        analyses_map = fetch_analyses(conn)
        feedbacks = fetch_feedbacks(conn)
        print(f"加载了 {len(ops)} 条交易操作, {len(analyses_map)} 条分析记录, {len(feedbacks)} 条反馈记录\n")
        analyze(ops, analyses_map, feedbacks)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
