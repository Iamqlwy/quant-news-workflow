#!/usr/bin/env python
"""买入信号质量综合评测脚本。

评估维度：
  1. 短期价格反应 — MFE/MAE、T+N 收益率分布
  2. 相对强度与 Alpha — 超额收益、IC 衰减、分组单调性
  3. 执行质量诊断 — 成交效率（如有成交数据）
  4. 策略健康度监控 — 滚动胜率、市场状态分层

输出：终端报告 + charts/ 目录下的可视化图表集。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import timedelta
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

# 分析周期
HORIZONS = [1, 3, 5, 10, 20, 30]  # 交易日

# 基准指数
BENCHMARKS = {"创业板指(399006)": "创业板指", "中证500(399905)": "中证500"}

# 图表输出目录
CHARTS_DIR = Path(__file__).resolve().parent / "charts"
CHARTS_DIR.mkdir(exist_ok=True)

# ── 数据库查询 ────────────────────────────────────────────────────────


def fetch_operations(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, operation_type, symbol, created_at, status,
               rationale, risk_level, trigger_analysis_id
        FROM trading_operations
        ORDER BY created_at
        """
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": str(r[0]),
            "operation_type": r[1],
            "symbol": r[2],
            "created_at": r[3],
            "status": r[4],
            "rationale": r[5],
            "risk_level": r[6],
            "trigger_analysis_id": str(r[7]) if r[7] else None,
        })
    cur.close()
    return rows


def fetch_analyses(conn) -> dict[str, dict]:
    """返回 {analysis_id: {confidence, time_horizon, analysis_type}} 的映射。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, confidence, time_horizon, analysis_type
        FROM analyses
        WHERE confidence IS NOT NULL
        """
    )
    result = {}
    for r in cur.fetchall():
        result[str(r[0])] = {
            "confidence": float(r[1]) if r[1] is not None else None,
            "time_horizon": r[2],
            "analysis_type": r[3],
        }
    cur.close()
    return result


# ── 日线数据 ──────────────────────────────────────────────────────────


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
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
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
    dates: list[str],
    klines: dict[str, dict],
    start_idx: int,
    buy_price: float,
) -> dict[int, Optional[dict]]:
    """计算买入后 n 个交易日的收益率、MFE、MAE。

    Returns: {horizon: {return_pct, mfe_pct, mae_pct, end_date} or None}
    """
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
        mae = (min_price - buy_price) / buy_price * 100  # 负值 = 亏损

        result[h] = {
            "return_pct": round(ret_pct, 2),
            "mfe_pct": round(mfe, 2),
            "mae_pct": round(mae, 2),
            "end_date": dates[end_idx],
        }

    return result


# ── 市场状态分类 ──────────────────────────────────────────────────────


def classify_market_state(
    index_klines: dict[str, dict],
    dates: list[str],
    target_date: str,
    lookback: int = 60,
) -> str:
    """根据基准指数过去 lookback 个交易日的表现分类市场状态。

    Returns: 'bull' / 'bear' / 'range'
    """
    # 找到 target_date 在 dates 中的位置
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

    first_close = segment[0]
    last_close = segment[-1]
    total_ret = (last_close - first_close) / first_close * 100

    if total_ret > 10:
        return "bull"
    elif total_ret < -10:
        return "bear"
    else:
        return "range"


# ── 核心计算 ──────────────────────────────────────────────────────────


def _stats(arr: list[float]) -> dict:
    """计算一组数值的基本统计量。"""
    if not arr:
        return {"n": 0, "mean": 0, "median": 0, "std": 0, "min": 0, "max": 0}
    a = np.array(arr)
    return {
        "n": len(a),
        "mean": float(np.mean(a)),
        "median": float(np.median(a)),
        "std": float(np.std(a, ddof=1)),
        "min": float(np.min(a)),
        "max": float(np.max(a)),
    }


def compute_mfe_mae_ratio(results: list[dict], horizon: int) -> dict:
    """计算指定 horizon 下的 MFE/MAE 比率统计。"""
    mfes = []
    maes = []
    ratios = []
    for r in results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        mfe = h_data["mfe_pct"]
        mae = abs(h_data["mae_pct"])  # mae 为负值，取绝对值
        mfes.append(mfe)
        maes.append(mae)
        if mae > 0:
            ratios.append(mfe / mae)
    return {
        "mfe": _stats([float(x) for x in mfes]),
        "mae": _stats([float(x) for x in maes]),
        "ratio_mean": float(np.mean(ratios)) if ratios else 0,
        "ratio_median": float(np.median(ratios)) if ratios else 0,
    }


def compute_excess_returns(
    buy_results: list[dict],
    bench_klines: dict[str, dict],
    bench_dates: list[str],
    horizon: int,
) -> list[float]:
    """计算买入标的的超额收益（已对冲基准）。"""
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


def compute_rank_ic(scores: np.ndarray, forward_returns: np.ndarray) -> float | None:
    """计算 Rank IC（斯皮尔曼秩相关系数）。"""
    mask = ~np.isnan(scores) & ~np.isnan(forward_returns)
    if mask.sum() < 10:
        return None
    from scipy.stats import spearmanr
    corr, _ = spearmanr(scores[mask], forward_returns[mask])
    return float(corr)


def compute_ic_sequence(
    buy_results: list[dict],
    analyses_map: dict[str, dict],
) -> dict[int, dict]:
    """计算各持有期的 IC 序列。

    使用 linked analyses 的 confidence 作为信号强度。
    """
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
        if ic is not None:
            result[h] = {"ic": ic, "n": len(scores_arr)}
        else:
            result[h] = {"ic": None, "n": len(scores_arr)}

    return result


# ════════════════════════════════════════════════════════════════════════
# 可视化
# ════════════════════════════════════════════════════════════════════════

import matplotlib
matplotlib.use("Agg")  # 无头后端
import matplotlib.pyplot as plt
import seaborn as sns

# 全局样式
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


def _safe_fname(s: str) -> str:
    return s.replace("/", "_").replace("\\", "_").replace(":", "_")


def plot_mfe_mae_scatter(buy_results: list[dict], horizon: int = 20):
    """MFE/MAE 散点图 — 核心质量诊断图。"""
    fig, ax = plt.subplots(figsize=(10, 8))

    mfes, maes, symbols, colors = [], [], [], []
    max_val = 0

    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        mfe = h_data["mfe_pct"]
        mae = h_data["mae_pct"]  # 负值
        mfes.append(mfe)
        maes.append(mae)
        symbols.append(r["symbol"] or "?")
        max_val = max(max_val, abs(mfe), abs(mae))

        # 象限着色
        if mfe > abs(mae) and mae > -5:  # 左上：低风险高回报
            colors.append(GREEN)
        elif mfe > abs(mae) and mae <= -5:  # 右上：高风险高回报
            colors.append(ORANGE)
        elif mfe <= abs(mae) and mae > -5:  # 左下：低风险低回报
            colors.append(BLUE)
        else:  # 右下：高风险低回报
            colors.append(RED)

    if not mfes:
        ax.text(0.5, 0.5, "数据不足", transform=ax.transAxes, ha="center", va="center")
        fig.savefig(CHARTS_DIR / "01_mfe_mae_scatter.png")
        plt.close(fig)
        return

    limit = max_val * 1.15
    mae_abs = [abs(m) for m in maes]

    ax.axhline(0, color="black", linewidth=0.5)
    ax.axvline(0, color="black", linewidth=0.5)
    # 对角线 MFE = |MAE|
    ax.plot([0, limit], [0, limit], color="gray", linestyle="--", alpha=0.5, label="MFE = |MAE| (盈亏平衡)")

    ax.scatter(mae_abs, mfes, c=colors, alpha=0.7, s=80, edgecolors="white", linewidth=0.5)

    # 象限标签
    ax.text(limit * 0.75, limit * 0.85, "高风险高回报", fontsize=10, color=ORANGE, ha="center", alpha=0.7)
    ax.text(limit * 0.25, limit * 0.85, "低风险高回报 ★理想", fontsize=10, color=GREEN, ha="center", alpha=0.7)
    ax.text(limit * 0.25, limit * 0.1, "低风险低回报", fontsize=10, color=BLUE, ha="center", alpha=0.7)
    ax.text(limit * 0.75, limit * 0.1, "高风险低回报 ✗淘汰", fontsize=10, color=RED, ha="center", alpha=0.7)

    ax.set_xlabel(f"|MAE| — 最大不利偏移 (T+{horizon}, %)", fontsize=13)
    ax.set_ylabel(f"MFE — 最大有利偏移 (T+{horizon}, %)", fontsize=13)
    ax.set_title(f"MFE / MAE 散点图 (N={len(mfes)}, T+{horizon})", fontsize=15, fontweight="bold")
    ax.set_xlim(-limit * 0.02, limit)
    ax.set_ylim(-limit * 0.02, limit)
    ax.legend(loc="upper left")
    ax.set_aspect("equal")

    # 标注极端点
    if mfes:
        best_idx = np.argmax(mfes)
        worst_idx = np.argmin([m for m in maes])  # 最深回撤
        ax.annotate(symbols[best_idx], (mae_abs[best_idx], mfes[best_idx]),
                    textcoords="offset points", xytext=(8, 8), fontsize=8, color=BLUE)
        ax.annotate(symbols[worst_idx], (mae_abs[worst_idx], mfes[worst_idx]),
                    textcoords="offset points", xytext=(8, -12), fontsize=8, color=RED)

    fig.savefig(CHARTS_DIR / "01_mfe_mae_scatter.png")
    plt.close(fig)
    print(f"  ✓ MFE/MAE 散点图已保存: charts/01_mfe_mae_scatter.png")


def plot_return_heatmap(buy_results: list[dict]):
    """T+N 收益率热力图 — 按交易日期 × 持有天数。"""
    fig, ax = plt.subplots(figsize=(14, max(8, len(buy_results) * 0.25)))

    buy_results = sorted(buy_results, key=lambda r: r["created_at"])
    rows = []
    for i, r in enumerate(buy_results):
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
    mask = np.isnan(data)

    # 颜色映射：红涨绿跌，以 0 为中心
    vmax = max(abs(np.nanpercentile(data, 2)), abs(np.nanpercentile(data, 98)), 1)
    cmap = sns.diverging_palette(130, 10, as_cmap=True)  # 绿涨红跌

    labels_y = [f"{r['symbol'] or '?'} {r['created_at'].strftime('%m-%d')}" for r in buy_results]
    labels_x = [f"T+{h}" for h in HORIZONS]

    sns.heatmap(data, mask=mask, cmap=cmap, center=0, vmin=-vmax, vmax=vmax,
                xticklabels=labels_x, yticklabels=labels_y,
                annot=True, fmt=".1f", linewidths=0.5, linecolor="white",
                cbar_kws={"label": "收益率 (%)"}, ax=ax)
    ax.set_title("T+N 收益率热力图", fontsize=15, fontweight="bold")
    ax.set_xlabel("持有天数")
    ax.set_ylabel("交易 (标的 + 日期)")

    fig.savefig(CHARTS_DIR / "02_return_heatmap.png", bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ 收益率热力图已保存: charts/02_return_heatmap.png")


def plot_return_boxplots(buy_results: list[dict]):
    """未来N日收益分布箱线图 — T+1 / T+5 / T+20。"""
    target_horizons = [1, 5, 20]
    fig, axes = plt.subplots(1, 3, figsize=(18, 7))

    for ax, h in zip(axes, target_horizons):
        rets = []
        for r in buy_results:
            h_data = r.get(f"h_{h}")
            if h_data is not None:
                rets.append(h_data["return_pct"])

        if not rets:
            ax.text(0.5, 0.5, "数据不足", transform=ax.transAxes, ha="center", va="center")
            continue

        bp = ax.boxplot(rets, widths=0.4, patch_artist=True,
                         medianprops={"color": "black", "linewidth": 2},
                         flierprops={"marker": "o", "markerfacecolor": RED, "alpha": 0.5, "markersize": 6})
        bp["boxes"][0].set_facecolor(BLUE)
        bp["boxes"][0].set_alpha(0.3)

        # 叠加散点
        jitter = np.random.normal(0, 0.06, len(rets))
        ax.scatter(np.ones(len(rets)) + jitter, rets, alpha=0.3, s=15, color=GRAY)

        # 关键统计
        med = np.median(rets)
        mean = np.mean(rets)
        win_rate = sum(1 for x in rets if x > 0) / len(rets) * 100

        ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
        ax.text(0.6, ax.get_ylim()[1] * 0.95,
                f"均值: {mean:+.2f}%\n中位数: {med:+.2f}%\n胜率: {win_rate:.0f}%\nN={len(rets)}",
                fontsize=10, va="top",
                bbox={"boxstyle": "round,pad=0.4", "facecolor": "white", "alpha": 0.8})

        ax.set_title(f"T+{h} 收益分布", fontsize=14, fontweight="bold")
        ax.set_ylabel("收益率 (%)")
        ax.set_xticklabels([])

    fig.suptitle("未来N日收益分布箱线图", fontsize=16, fontweight="bold", y=1.01)
    fig.tight_layout()
    fig.savefig(CHARTS_DIR / "03_return_boxplots.png")
    plt.close(fig)
    print(f"  ✓ 收益分布箱线图已保存: charts/03_return_boxplots.png")


def plot_group_excess_curves(
    buy_results: list[dict],
    analyses_map: dict[str, dict],
    bench_data: dict[str, dict],
    bench_dates: list[str],
    bench_name: str,
):
    """多空分组超额收益曲线 — 按信号强度分5组。"""
    # 分配信号分数
    scored = []
    for r in buy_results:
        score = None
        if r["trigger_analysis_id"] and r["trigger_analysis_id"] in analyses_map:
            score = analyses_map[r["trigger_analysis_id"]].get("confidence")
        if score is not None:
            scored.append((score, r))
    if len(scored) < 10:
        print(f"  ⚠ 有信号分数的样本不足 ({len(scored)}), 跳过多空分组图")
        return

    scored.sort(key=lambda x: x[0])
    n = len(scored)
    group_size = n // 5

    # 为每组计算每个 horizon 的平均超额收益（对冲 bench_name）
    group_labels = []
    group_excess_by_h: dict[int, list[float]] = defaultdict(list)

    for g in range(5):
        start = g * group_size
        end = start + group_size if g < 4 else n
        group = scored[start:end]
        group_labels.append(f"G{g + 1} (n={len(group)}, conf={group[0][0]:.2f}–{group[-1][0]:.2f})")

        for h in HORIZONS:
            excess_vals = []
            for _, r in group:
                h_data = r.get(f"h_{h}")
                if h_data is None:
                    continue
                effective_date = r["effective_buy_date"]
                b_close, _, b_idx = get_close_on_or_after(bench_dates, bench_data, effective_date)
                if b_close is None:
                    continue
                b_rets = get_next_n_returns(bench_dates, bench_data, b_idx, b_close)
                if b_rets.get(h) is None:
                    continue
                excess_vals.append(h_data["return_pct"] - b_rets[h]["return_pct"])
            group_excess_by_h[h].append(np.mean(excess_vals) if excess_vals else None)

    fig, ax = plt.subplots(figsize=(12, 7))
    palette = sns.color_palette("RdYlGn", 5)

    for g in range(5):
        y_vals = [group_excess_by_h[h][g] for h in HORIZONS]
        valid_x = [h for h, y in zip(HORIZONS, y_vals) if y is not None]
        valid_y = [y for y in y_vals if y is not None]
        if valid_y:
            ax.plot(valid_x, valid_y, marker="o", linewidth=2.5, markersize=8,
                    color=palette[4 - g], label=group_labels[g])

    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.set_xlabel("持有天数", fontsize=13)
    ax.set_ylabel(f"超额收益 vs {bench_name} (%)", fontsize=13)
    ax.set_title(f"多空分组超额收益曲线 (vs {bench_name})", fontsize=15, fontweight="bold")
    ax.legend(fontsize=9, loc="best")
    ax.set_xticks(HORIZONS)

    fig.savefig(CHARTS_DIR / f"04_group_excess_{_safe_fname(bench_name)}.png")
    plt.close(fig)
    print(f"  ✓ 分组超额收益曲线已保存: charts/04_group_excess_{_safe_fname(bench_name)}.png")


def plot_ic_decay(ic_sequence: dict[int, dict]):
    """信息系数（IC）衰减曲线。"""
    hs = sorted(ic_sequence.keys())
    ics = [ic_sequence[h]["ic"] for h in hs]

    valid_hs = [h for h, ic in zip(hs, ics) if ic is not None]
    valid_ics = [ic for ic in ics if ic is not None]

    if not valid_ics:
        print("  ⚠ 无有效 IC 数据，跳过 IC 衰减图")
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    ax.plot(valid_hs, valid_ics, marker="o", linewidth=2.5, markersize=10, color=BLUE,
            label="Rank IC (Spearman)")

    # 标注
    for h, ic in zip(valid_hs, valid_ics):
        ax.annotate(f"{ic:.3f}", (h, ic), textcoords="offset points",
                    xytext=(0, 12), fontsize=9, ha="center", color=BLUE)

    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.fill_between(valid_hs, 0, valid_ics, alpha=0.1, color=BLUE)
    ax.set_xlabel("预测周期 (交易日)", fontsize=13)
    ax.set_ylabel("Rank IC", fontsize=13)
    ax.set_title("信息系数 (IC) 衰减曲线\n信号强度 vs 未来超额收益", fontsize=15, fontweight="bold")
    ax.set_xticks(HORIZONS)
    ax.legend()

    fig.savefig(CHARTS_DIR / "05_ic_decay.png")
    plt.close(fig)
    print(f"  ✓ IC 衰减曲线已保存: charts/05_ic_decay.png")


def plot_rolling_win_rate(buy_results: list[dict], window: int = 60, horizon: int = 20):
    """滚动胜率/盈亏比曲线。"""
    buy_results = sorted(buy_results, key=lambda r: r["created_at"])
    if len(buy_results) < window:
        print(f"  ⚠ 样本不足 ({len(buy_results)} < window {window}), 跳过滚动曲线")
        return

    rets = []
    dates = []
    mfes = []
    maes = []
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is not None:
            rets.append(h_data["return_pct"])
            mfes.append(h_data["mfe_pct"])
            maes.append(abs(h_data["mae_pct"]))
        else:
            rets.append(None)
            mfes.append(None)
            maes.append(None)
        dates.append(r["created_at"])

    rolling_wr = []
    rolling_pl = []  # profit/loss ratio
    rolling_dates = []

    for i in range(window - 1, len(rets)):
        segment_ret = [r for r in rets[i - window + 1:i + 1] if r is not None]
        segment_mfe = [m for m in mfes[i - window + 1:i + 1] if m is not None]
        segment_mae = [m for m in maes[i - window + 1:i + 1] if m is not None]
        if not segment_ret:
            continue
        wr = sum(1 for x in segment_ret if x > 0) / len(segment_ret) * 100
        rolling_wr.append(wr)
        if segment_mfe and segment_mae:
            pl = np.mean(segment_mfe) / np.mean(segment_mae)
            rolling_pl.append(pl)
        else:
            rolling_pl.append(None)
        rolling_dates.append(dates[i])

    if not rolling_wr:
        return

    fig, ax1 = plt.subplots(figsize=(14, 7))
    ax2 = ax1.twinx()

    ax1.plot(rolling_dates, rolling_wr, color=BLUE, linewidth=2, label=f"滚动胜率 ({window}日)")
    ax1.axhline(50, color="gray", linestyle="--", alpha=0.5, label="50% 基准")

    valid_pl = [(d, p) for d, p in zip(rolling_dates, rolling_pl) if p is not None]
    if valid_pl:
        pl_dates, pl_vals = zip(*valid_pl)
        ax2.plot(pl_dates, pl_vals, color=ORANGE, linewidth=2, linestyle="--",
                 label=f"盈亏比 (MFE/|MAE|)")

    ax1.set_xlabel("日期", fontsize=13)
    ax1.set_ylabel("胜率 (%)", color=BLUE, fontsize=13)
    ax2.set_ylabel("盈亏比", color=ORANGE, fontsize=13)
    ax1.tick_params(axis="y", labelcolor=BLUE)
    ax2.tick_params(axis="y", labelcolor=ORANGE)
    ax1.set_title(f"滚动胜率 / 盈亏比曲线 (T+{horizon}, {window}日窗口)", fontsize=15, fontweight="bold")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    fig.autofmt_xdate()
    fig.savefig(CHARTS_DIR / "06_rolling_win_rate.png")
    plt.close(fig)
    print(f"  ✓ 滚动胜率曲线已保存: charts/06_rolling_win_rate.png")


def plot_market_state_bars(
    buy_results: list[dict],
    bench_data: dict[str, dict],
    bench_dates: list[str],
    bench_name: str,
    horizon: int = 5,
):
    """市场状态分层收益柱状图。"""
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
    labels = []
    means = []
    errors = []
    significances = []
    n_values = []

    for s in order:
        vals = states.get(s, [])
        if not vals:
            continue
        labels.append({"bull": "牛市", "range": "震荡市", "bear": "熊市", "unknown": "未知"}.get(s, s))
        means.append(np.mean(vals))
        errors.append(np.std(vals, ddof=1) / np.sqrt(len(vals)))
        n_values.append(len(vals))
        # 单样本 t 检验: H0: mean = 0
        try:
            _, p = ttest_1samp(vals, 0)
        except Exception:
            p = 1.0
        sig = ""
        if p < 0.01:
            sig = "**"
        elif p < 0.05:
            sig = "*"
        significances.append(sig)

    fig, ax = plt.subplots(figsize=(10, 7))
    bars = ax.bar(labels, means, yerr=errors, capsize=8,
                  color=[GREEN if m > 0 else RED for m in means],
                  alpha=0.7, edgecolor="white", linewidth=1.2)

    for bar, mean, sig, n in zip(bars, means, significances, n_values):
        y_pos = mean + (0.3 if mean >= 0 else -0.8)
        ax.text(bar.get_x() + bar.get_width() / 2, y_pos,
                f"{mean:+.2f}%{sig}\n(n={n})",
                ha="center", va="center", fontsize=10, fontweight="bold")

    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel(f"平均 T+{horizon} 收益 (%)", fontsize=13)
    ax.set_title(f"市场状态分层收益 (T+{horizon}, vs {bench_name})\n*p<0.05, **p<0.01", fontsize=15, fontweight="bold")

    fig.savefig(CHARTS_DIR / "07_market_state_bars.png")
    plt.close(fig)
    print(f"  ✓ 市场状态分层柱状图已保存: charts/07_market_state_bars.png")


def plot_confidence_group_monotonicity(
    buy_results: list[dict],
    analyses_map: dict[str, dict],
    horizon: int = 20,
):
    """信号强度分组单调性测试 — 分组收益棒棒糖图。"""
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
        print(f"  ⚠ 样本不足 ({len(scored)}), 跳过单调性测试图")
        return

    scored.sort(key=lambda x: x[0])
    n = len(scored)
    n_groups = min(5, n // 3)
    group_size = n // n_groups

    group_means = []
    group_labels = []
    all_returns = [x[1] for x in scored]

    for g in range(n_groups):
        start = g * group_size
        end = start + group_size if g < n_groups - 1 else n
        rets = [x[1] for x in scored[start:end]]
        group_means.append(np.mean(rets))
        group_labels.append(f"G{g + 1}\n(低信)" if g == 0 else
                            f"G{g + 1}" if g < n_groups - 1 else
                            f"G{g + 1}\n(高信)")

    fig, ax = plt.subplots(figsize=(10, 6))

    # 棒棒糖图
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
    ax.set_xticklabels(group_labels, fontsize=10)
    ax.set_ylabel(f"平均 T+{horizon} 收益 (%)", fontsize=13)
    ax.set_title(f"信号强度分组单调性测试 (T+{horizon})\n按 confidence 从低到高分组", fontsize=15, fontweight="bold")
    ax.legend()

    # 输出单调性结论
    from scipy.stats import spearmanr
    group_ranks = list(range(1, n_groups + 1))
    try:
        rho, p = spearmanr(group_ranks, group_means)
        conclusion = f"Spearman ρ = {rho:.3f} (p = {p:.3f}) — "
        if rho > 0.3 and p < 0.1:
            conclusion += "呈正向单调 ✓"
        elif rho < -0.3 and p < 0.1:
            conclusion += "呈反向单调 ✗"
        else:
            conclusion += "无显著单调性"
    except Exception:
        conclusion = "无法计算单调性"
    ax.text(0.5, -0.12, conclusion, transform=ax.transAxes, fontsize=11,
            ha="center", fontstyle="italic")

    fig.savefig(CHARTS_DIR / "08_confidence_monotonicity.png")
    plt.close(fig)
    print(f"  ✓ 单调性测试图已保存: charts/08_confidence_monotonicity.png")
    print(f"    {conclusion}")


# ════════════════════════════════════════════════════════════════════════
# 主分析流程
# ════════════════════════════════════════════════════════════════════════


def analyze(ops: list[dict], analyses_map: dict[str, dict]):
    # ── 数据预处理 ──
    klines_cache: dict[str, dict[str, dict]] = {}
    dates_cache: dict[str, list[str]] = {}

    def _ensure_klines(sym: str):
        if sym not in klines_cache:
            klines_cache[sym] = load_klines(sym)
            dates_cache[sym] = _sorted_dates(klines_cache[sym])

    buys = [o for o in ops if o["operation_type"] == "buy" and o["symbol"]]
    # 按 symbol 去重，每只股票只保留最早的一条 buy 记录
    buys_deduped: dict[str, dict] = {}
    for o in buys:
        sym = o["symbol"]
        if sym not in buys_deduped or o["created_at"] < buys_deduped[sym]["created_at"]:
            buys_deduped[sym] = o
    buys = list(buys_deduped.values())
    if len(buys) < len(buys_deduped):
        print(f"  buy 去重: {len(buys_deduped)} 条 → {len(buys)} 条")

    sells = [o for o in ops if o["operation_type"] == "sell" and o["symbol"]]
    skips = [o for o in ops if o["operation_type"] == "skip" and o["symbol"]]

    symbols_needed = set(o["symbol"] for o in buys + skips)
    print(f"加载日线数据 {len(symbols_needed)} 个标的...")
    for i, sym in enumerate(sorted(symbols_needed)):
        _ensure_klines(sym)
        if (i + 1) % 20 == 0 or i + 1 == len(symbols_needed):
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
    if len(buys_filtered) < len(buys):
        print(f"  过滤掉 {len(buys) - len(buys_filtered)} 条 buy（数据不足）")

    buys_approved = [o for o in buys_filtered if o["status"] == "approved"]
    buys_rejected = [o for o in buys_filtered if o["status"] == "rejected"]
    buys_pending = [o for o in buys_filtered if o["status"] == "pending"]
    buys_triggered_close = [o for o in buys_filtered if o["status"] == "triggered_close"]

    print()
    print(f"总记录: {len(ops)}")
    print(f"  buy : {len(buys_filtered)} (approved={len(buys_approved)}, rejected={len(buys_rejected)}, "
          f"pending={len(buys_pending)}, triggered_close={len(buys_triggered_close)})")
    print(f"  sell: {len(sells)}")
    print(f"  skip: {len(skips_filtered)}")
    print()

    # ── 加载基准指数 ──
    benchmark_data: dict[str, dict[str, dict]] = {}
    benchmark_dates: dict[str, list[str]] = {}
    print(f"加载基准指数: {list(BENCHMARKS.keys())}")
    for name, code in BENCHMARKS.items():
        benchmark_data[name] = load_klines(code)
        benchmark_dates[name] = _sorted_dates(benchmark_data[name])
        print(f"  {name} ({code}): {len(benchmark_data[name])} 个交易日")
    print()

    # ── 对所有 buy 计算完整指标 ──
    print("计算买入后收益指标 (含 MFE/MAE)...")
    buy_results: list[dict] = []
    for o in buys_filtered:
        klines = klines_cache[o["symbol"]]
        effective_date = _effective_buy_date(o["created_at"])
        dates = dates_cache[o["symbol"]]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue

        rets = get_next_n_returns(dates, klines, idx, close)
        # 判断市场状态
        cy_klines = benchmark_data.get("创业板指(399006)", {})
        cy_dates = benchmark_dates.get("创业板指(399006)", [])
        market_state = classify_market_state(cy_klines, cy_dates, found_date) if cy_klines else "unknown"

        entry = {
            "symbol": o["symbol"],
            "created_at": o["created_at"],
            "status": o["status"],
            "risk_level": o.get("risk_level") or "unknown",
            "trigger_analysis_id": o.get("trigger_analysis_id"),
            "buy_price": close,
            "buy_date": found_date,
            "effective_buy_date": effective_date,
            "market_state": market_state,
        }
        for h in HORIZONS:
            entry[f"h_{h}"] = rets.get(h)
        buy_results.append(entry)
    print(f"  有效买入: {len(buy_results)} 条")
    print()

    # ═══════════════════════════════════════════
    # A. 短期价格反应指标
    # ═══════════════════════════════════════════
    print("=" * 80)
    print("一、短期价格反应 — T+N 收益率分布 & MFE/MAE")
    print("=" * 80)

    for h in HORIZONS:
        rets = [r[f"h_{h}"]["return_pct"] for r in buy_results if r.get(f"h_{h}") is not None]
        if not rets:
            continue
        stats = _stats(rets)
        mfe_mae = compute_mfe_mae_ratio(buy_results, h)
        win = sum(1 for x in rets if x > 0)

        print(f"\n--- T+{h} 个交易日 (N={len(rets)}) ---")
        print(f"  收益率:")
        print(f"    均值: {stats['mean']:+.2f}%    中位数: {stats['median']:+.2f}%")
        print(f"    标准差: {stats['std']:.2f}%    最佳: {stats['max']:+.2f}%    最差: {stats['min']:+.2f}%")
        print(f"    胜率: {win / len(rets) * 100:.1f}% ({win}/{len(rets)})")
        print(f"  MFE (最大有利偏移):")
        print(f"    均值: {mfe_mae['mfe']['mean']:+.2f}%    中位数: {mfe_mae['mfe']['median']:+.2f}%    最大: {mfe_mae['mfe']['max']:+.2f}%")
        print(f"  MAE (最大不利偏移):")
        print(f"    均值: {mfe_mae['mae']['mean']:+.2f}%    中位数: {mfe_mae['mae']['median']:+.2f}%    最小(最深): {mfe_mae['mae']['min']:+.2f}%")
        print(f"  MFE/MAE 比率: 均值 {mfe_mae['ratio_mean']:.2f}  中位数 {mfe_mae['ratio_median']:.2f}  {'← 良好' if mfe_mae['ratio_mean'] > 1 else '← 偏低'}")

    # ═══════════════════════════════════════════
    # B. 相对强度与 Alpha 指标
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("二、相对强度与 Alpha — 超额收益 & IC 分析")
    print("=" * 80)

    # 超额收益
    for h in HORIZONS:
        buy_rets = [r[f"h_{h}"]["return_pct"] for r in buy_results if r.get(f"h_{h}") is not None]
        for name in BENCHMARKS:
            excess = compute_excess_returns(buy_results, benchmark_data[name], benchmark_dates[name], h)
            if excess:
                exc_stats = _stats(excess)
                print(f"  T+{h} vs {name}: 超额收益均值 {exc_stats['mean']:+.2f}%, "
                      f"中位数 {exc_stats['median']:+.2f}%, 胜率 {sum(1 for x in excess if x > 0) / len(excess) * 100:.1f}%")

    # IC 分析
    print("\n--- 信息系数 (IC) 分析 ---")
    ic_seq = compute_ic_sequence(buy_results, analyses_map)
    for h in HORIZONS:
        info = ic_seq.get(h, {})
        ic = info.get("ic")
        if ic is not None:
            print(f"  T+{h}: Rank IC = {ic:+.4f} (N={info['n']}) {'✓ 有效' if abs(ic) > 0.05 else '✗ 微弱'}")
        else:
            print(f"  T+{h}: 数据不足")

    # 分组单调性测试
    print("\n--- 分组单调性测试 (按 confidence 分5组, T+20) ---")
    scored = []
    for r in buy_results:
        h20 = r.get("h_20")
        if h20 is None:
            continue
        score = None
        if r["trigger_analysis_id"] and r["trigger_analysis_id"] in analyses_map:
            score = analyses_map[r["trigger_analysis_id"]].get("confidence")
        if score is not None:
            scored.append((score, h20["return_pct"]))

    if len(scored) >= 15:
        scored.sort(key=lambda x: x[0])
        n = len(scored)
        n_groups = min(5, n // 3)
        group_size = n // n_groups
        print(f"  样本: {n}, 分 {n_groups} 组, 每组约 {group_size} 条")
        print(f"  {'分组':<12s} {'区间':<20s} {'平均收益':>10s} {'胜率':>8s}")
        print(f"  {'-' * 52}")
        group_means = []
        for g in range(n_groups):
            start = g * group_size
            end = start + group_size if g < n_groups - 1 else n
            group_ret = [x[1] for x in scored[start:end]]
            avg = np.mean(group_ret)
            wr = sum(1 for x in group_ret if x > 0) / len(group_ret) * 100
            conf_range = f"{scored[start][0]:.2f}–{scored[end - 1][0]:.2f}"
            g_label = f"G{g + 1}"
            print(f"  {g_label:<12s} {conf_range:<20s} {avg:+9.2f}%  {wr:7.1f}%")
            group_means.append(avg)

        from scipy.stats import spearmanr
        rho, p = spearmanr(range(1, n_groups + 1), group_means)
        print(f"  Spearman ρ: {rho:.3f} (p={p:.3f}) — {'正向单调 ✓' if rho > 0.3 and p < 0.1 else '无显著单调性'}")
    else:
        print(f"  样本不足 ({len(scored)}), 跳过")

    # ═══════════════════════════════════════════
    # C. Buy vs Skip vs Benchmark 综合对比
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("三、Buy Approved vs Rejected vs Benchmark 综合对比 (T+30)")
    print("=" * 80)

    status_results: dict[str, list[dict]] = {
        "approved": [r for r in buy_results if r["status"] == "approved"],
        "rejected": [r for r in buy_results if r["status"] == "rejected"],
        "pending": [r for r in buy_results if r["status"] == "pending"],
        "triggered_close": [r for r in buy_results if r["status"] == "triggered_close"],
    }

    for name in BENCHMARKS:
        print(f"\n--- vs {name} ---")
        print(f"  {'分组':<18s} {'N':>4s}  {'T+30收益':>9s}  {'MFE均值':>8s}  {'|MAE|均值':>8s}  {'MFE/MAE':>8s}  {'超额':>8s}")
        print(f"  {'-' * 75}")

        for label in ["approved", "rejected", "pending", "triggered_close"]:
            group = status_results[label]
            rets = [r["h_30"]["return_pct"] for r in group if r.get("h_30") is not None]
            mfe_mae = compute_mfe_mae_ratio(group, 30)
            excess = compute_excess_returns(group, benchmark_data[name], benchmark_dates[name], 30)
            if rets:
                avg_r = np.mean(rets)
                ex_avg = np.mean(excess) if excess else 0
                print(f"  {label:<18s} {len(rets):4d}  {avg_r:+8.2f}%  {mfe_mae['mfe']['mean']:+7.2f}%  "
                      f"{mfe_mae['mae']['mean']:+7.2f}%  {mfe_mae['ratio_mean']:+7.2f}  {ex_avg:+7.2f}%")

    # ═══════════════════════════════════════════
    # D. Skip 决策质量
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("四、Skip 决策质量分析")
    print("=" * 80)

    skip_results: list[dict] = []
    for o in skips_filtered:
        klines = klines_cache[o["symbol"]]
        effective_date = _effective_buy_date(o["created_at"])
        dates = dates_cache[o["symbol"]]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue
        rets = get_next_n_returns(dates, klines, idx, close)
        entry = {
            "symbol": o["symbol"],
            "created_at": o["created_at"],
            "buy_price": close,
            "buy_date": found_date,
        }
        for h in HORIZONS:
            entry[f"h_{h}"] = rets.get(h)
        skip_results.append(entry)

    for h in HORIZONS:
        s_rets = [r[f"h_{h}"]["return_pct"] for r in skip_results if r.get(f"h_{h}") is not None]
        if not s_rets:
            continue
        missed_up5 = sum(1 for x in s_rets if x > 5)
        missed_up10 = sum(1 for x in s_rets if x > 10)
        avoided5 = sum(1 for x in s_rets if x < -5)
        avoided10 = sum(1 for x in s_rets if x < -10)
        print(f"\n  T+{h} (N={len(s_rets)}):")
        print(f"    正确跳过(跌>5%):  {avoided5:3d} ({avoided5 / len(s_rets) * 100:5.1f}%)")
        print(f"    正确跳过(跌>10%): {avoided10:3d} ({avoided10 / len(s_rets) * 100:5.1f}%)")
        print(f"    错过涨幅(涨>5%):  {missed_up5:3d} ({missed_up5 / len(s_rets) * 100:5.1f}%)")
        print(f"    错过涨幅(涨>10%): {missed_up10:3d} ({missed_up10 / len(s_rets) * 100:5.1f}%)")

    # ═══════════════════════════════════════════
    # E. 市场状态分层
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("五、市场状态分层收益")
    print("=" * 80)

    states: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        h20 = r.get("h_20")
        if h20 is not None:
            states[r.get("market_state", "unknown")].append(h20["return_pct"])

    for s in ["bull", "range", "bear", "unknown"]:
        vals = states.get(s, [])
        if vals:
            stats = _stats(vals)
            print(f"  {s:>8s} (N={len(vals):3d}): T+20 均值 {stats['mean']:+.2f}%, "
                  f"中位数 {stats['median']:+.2f}%, 胜率 {sum(1 for x in vals if x > 0) / len(vals) * 100:.1f}%")

    # ═══════════════════════════════════════════
    # 生成图表
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("生成可视化图表...")
    print("=" * 80)

    plot_mfe_mae_scatter(buy_results, horizon=20)
    plot_return_heatmap(buy_results)
    plot_return_boxplots(buy_results)

    for name in BENCHMARKS:
        plot_group_excess_curves(buy_results, analyses_map,
                                 benchmark_data[name], benchmark_dates[name], name)

    plot_ic_decay(ic_seq)
    plot_rolling_win_rate(buy_results, window=min(60, len(buy_results)), horizon=20)
    plot_market_state_bars(buy_results, benchmark_data.get("创业板指(399006)", {}),
                           benchmark_dates.get("创业板指(399006)", []), "创业板指", horizon=5)
    plot_confidence_group_monotonicity(buy_results, analyses_map, horizon=20)

    print(f"\n所有图表已保存到: {CHARTS_DIR.resolve()}")


# ── 主入口 ────────────────────────────────────────────────────────────


def main():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
    )
    conn.set_client_encoding("UTF8")

    try:
        ops = fetch_operations(conn)
        analyses_map = fetch_analyses(conn)
        print(f"加载了 {len(ops)} 条交易操作, {len(analyses_map)} 条分析记录")
        analyze(ops, analyses_map)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
