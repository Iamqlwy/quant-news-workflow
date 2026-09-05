#!/usr/bin/env python
"""回测评估报告 v2 — 仅 BUY 操作深度分析。

深度整合 quant_kb 数据库全部关联表 + 本地数据，生成：
  1. 每只标的的独立分析卡
  2. 决策链路追踪（新闻→分析→交易→反馈）
  3. 多维交叉分析矩阵
  4. Markdown 报告 + 图表 + 个股明细CSV
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
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
INDICATOR_DIR = Path("C:/klines/indicator")
CONCEPTS_PATH = Path("C:/klines/concepts/stock_concepts.csv")
CONCEPT_CATALOG_PATH = Path("C:/klines/concepts/concept.csv")
INDUSTRY_PATH = Path("C:/klines/concepts/industry.csv")
CONCEPT_KLINES_DIR = Path("C:/klines/concepts/kline")
CONCEPT_MEMBER_DIR = Path("C:/klines/concepts/member")
ZDT_DIR = Path("C:/klines/extra/zdt")
BASIC_DIR = Path("C:/klines/extra/all_daily_basic")
STOCK_BASIC_PATH = Path("C:/klines/stock_basic.csv")
COMPANY_DIR = Path("C:/klines/companys")

HORIZONS = [1, 3, 5, 10, 20, 30]
BENCHMARKS = [("创业板指", "创业板指"), ("中证500", "中证500"), ("沪深300", "沪深300")]
RISK_FREE_RATE = 0.02

NOW = datetime.now().strftime("%Y%m%d_%H%M%S")
CHARTS_DIR = Path(__file__).resolve().parent / "charts"
CHARTS_DIR.mkdir(exist_ok=True)
REPORT_PATH = Path(__file__).resolve().parent / f"backtest_report_v2_{NOW}.md"
CSV_PATH = Path(__file__).resolve().parent / f"per_stock_details_{NOW}.csv"

# ── Matplotlib ────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.dates import DateFormatter, AutoDateLocator
import seaborn as sns

sns.set_theme(style="whitegrid", context="talk")
plt.rcParams.update({
    "font.sans-serif": ["SimHei", "Microsoft YaHei", "DejaVu Sans"],
    "axes.unicode_minus": False, "figure.dpi": 150, "savefig.dpi": 150,
    "savefig.bbox": "tight", "savefig.facecolor": "white",
})

RED, GREEN, BLUE, ORANGE, PURPLE, GRAY = "#E74C3C", "#27AE60", "#2980B9", "#E67E22", "#8E44AD", "#95A5A6"

# ════════════════════════════════════════════════════════════════════════
# 数据库查询
# ════════════════════════════════════════════════════════════════════════

def fetch_all_data(conn):
    """一次性加载所有需要的数据，减少数据库往返。"""
    cur = conn.cursor()

    # 1. BUY 操作 + JOIN analyses
    cur.execute("""
        SELECT t.id, t.operation_type, t.symbol, t.created_at, t.status,
               t.rationale, t.risk_level, t.trigger_analysis_id, t.target_node_id,
               t.price, t.quantity, t.expected_impact,
               a.id as aid, a.title as a_title, a.content as a_content,
               a.confidence, a.time_horizon, a.analysis_type, a.root_raw_info_ids
        FROM trading_operations t
        LEFT JOIN analyses a ON t.trigger_analysis_id = a.id
        WHERE t.operation_type = 'buy'
        ORDER BY t.created_at
    """)
    buys = []
    for r in cur.fetchall():
        buys.append({
            "id": str(r[0]), "symbol": r[2], "created_at": r[3], "status": r[4],
            "rationale": r[5], "risk_level": r[6], "trigger_analysis_id": str(r[7]) if r[7] else None,
            "target_node_id": str(r[8]) if r[8] else None,
            "price": float(r[9]) if r[9] else None, "quantity": float(r[10]) if r[10] else None,
            "expected_impact": r[11],
            "analysis_id": str(r[12]) if r[12] else None,
            "analysis_title": r[13], "analysis_content": r[14],
            "confidence": float(r[15]) if r[15] is not None else None,
            "time_horizon": r[16], "analysis_type": r[17],
            "root_raw_info_ids": r[18] if r[18] else [],
        })

    # 2. world_nodes (只加载BUY相关的)
    node_ids = set(b["target_node_id"] for b in buys if b["target_node_id"])
    if node_ids:
        placeholders = ",".join(["%s"] * len(node_ids))
        cur.execute(f"""
            SELECT wn.id, wn.name, wn.node_type, wn.description, wn.ticker
            FROM world_nodes wn WHERE wn.id IN ({placeholders})
        """, list(node_ids))
        wn_map = {}
        for r in cur.fetchall():
            wn_map[str(r[0])] = {"name": r[1], "node_type": r[2], "description": r[3], "ticker": r[4]}
    else:
        wn_map = {}

    # 3. node_states (最新版本)
    if node_ids:
        cur.execute(f"""
            SELECT DISTINCT ON (ns.node_id) ns.node_id, ns.core_logic, ns.primary_drivers, ns.risks, ns.focus_points
            FROM node_states ns WHERE ns.node_id IN ({placeholders})
            ORDER BY ns.node_id, ns.version DESC
        """, list(node_ids))
        ns_map = {}
        for r in cur.fetchall():
            ns_map[str(r[0])] = {
                "core_logic": r[1], "primary_drivers": r[2] if isinstance(r[2], str) else json.dumps(r[2], ensure_ascii=False) if r[2] else None,
                "risks": r[3] if isinstance(r[3], str) else json.dumps(r[3], ensure_ascii=False) if r[3] else None,
                "focus_points": r[4] if isinstance(r[4], str) else json.dumps(r[4], ensure_ascii=False) if r[4] else None,
            }
    else:
        ns_map = {}

    # 4. feedbacks (只加载BUY相关的)
    buy_ids = [b["id"] for b in buys]
    if buy_ids:
        placeholders2 = ",".join(["%s"] * len(buy_ids))
        cur.execute(f"""
            SELECT id, trigger_trade_id, judgment_correct, error_reason,
                   missed_factors, adjustment_suggestions, lessons_learned, created_at
            FROM feedbacks WHERE trigger_trade_id IN ({placeholders2})
        """, list(buy_ids))
        fb_map: dict[str, list] = defaultdict(list)
        for r in cur.fetchall():
            fb_map[str(r[1])].append({
                "id": str(r[0]), "judgment_correct": r[2],
                "error_reason": r[3], "missed_factors": r[4],
                "adjustment_suggestions": r[5], "lessons_learned": r[6],
                "created_at": r[7],
            })
    else:
        fb_map = {}

    # 5. raw_information 标题（仅用于有 root_raw_info_ids 的 analyses）
    all_raw_ids = set()
    for b in buys:
        raw_ids = b.get("root_raw_info_ids")
        if raw_ids and isinstance(raw_ids, str):
            # psycopg2 returns UUID array as "{uuid1,uuid2,...}" string
            raw_ids = raw_ids.strip("{}").split(",")
            raw_ids = [r.strip() for r in raw_ids if r.strip()]
            b["root_raw_info_ids"] = raw_ids
        if raw_ids and isinstance(raw_ids, list):
            for rid in raw_ids:
                if rid:
                    all_raw_ids.add(rid)
    if all_raw_ids:
        placeholders3 = ",".join(["%s"] * len(all_raw_ids))
        cur.execute(f"""
            SELECT id, title, source, published_at FROM raw_information WHERE id IN ({placeholders3})
        """, list(all_raw_ids))
        raw_map = {}
        for r in cur.fetchall():
            raw_map[str(r[0])] = {"title": r[1], "source": r[2], "published_at": r[3]}
    else:
        raw_map = {}

    cur.close()

    return buys, wn_map, ns_map, fb_map, raw_map


# ════════════════════════════════════════════════════════════════════════
# 本地数据加载
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
    result = {}
    for h in HORIZONS:
        end_idx = start_idx + h
        if end_idx >= len(dates):
            result[h] = None
            continue
        end_close = klines[dates[end_idx]]["close"]
        ret_pct = (end_close - buy_price) / buy_price * 100
        max_price, min_price = buy_price, buy_price
        for j in range(start_idx, end_idx + 1):
            price = klines[dates[j]]["close"]
            if price > max_price:
                max_price = price
            if price < min_price:
                min_price = price
        result[h] = {
            "return_pct": round(ret_pct, 2),
            "mfe_pct": round((max_price - buy_price) / buy_price * 100, 2),
            "mae_pct": round((min_price - buy_price) / buy_price * 100, 2),
            "end_date": dates[end_idx],
        }
    return result


def load_indicator(symbol: str) -> dict[str, dict]:
    path = INDICATOR_DIR / f"{symbol}.csv"
    if not path.exists():
        return {}
    data: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = _normalize_date(row["trade_date"])
            try:
                data[d] = {
                    "pe": float(row["pe"]) if row.get("pe") and float(row["pe"]) > 0 else None,
                    "pe_ttm": float(row["pe_ttm"]) if row.get("pe_ttm") and float(row["pe_ttm"]) > 0 else None,
                    "pb": float(row["pb"]) if row.get("pb") and float(row["pb"]) > 0 else None,
                    "total_mv": float(row["total_mv"]) if row.get("total_mv") else None,
                    "circ_mv": float(row["circ_mv"]) if row.get("circ_mv") else None,
                    "turnover_rate": float(row["turnover_rate"]) if row.get("turnover_rate") else None,
                }
            except (ValueError, TypeError):
                pass
    return data


def load_stock_concepts() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = defaultdict(list)
    if not CONCEPTS_PATH.exists():
        return mapping
    with open(CONCEPTS_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_code = (row.get("con_code") or "").strip()
            if not ts_code:
                continue
            all_c = row.get("all_concepts", "") or ""
            if all_c:
                mapping[ts_code] = [c.strip() for c in all_c.split("|") if c.strip()]
    return mapping


def load_company_json(symbol: str) -> Optional[dict]:
    """加载公司档案 JSON。文件名格式: 600519.SH_贵州茅台.json"""
    candidates = list(COMPANY_DIR.glob(f"{symbol}_*.json"))
    if not candidates:
        return None
    with open(candidates[0], encoding="utf-8") as f:
        data = json.load(f)
    return {
        "name": data.get("公司简介", "").split("。")[0] if data.get("公司简介") else "",
        "industry_dongcai": data.get("所属东财行业", ""),
        "main_business": data.get("主营业务", ""),
        "scope": data.get("经营范围", ""),
        "core_competitiveness": data.get("核心竞争力", ""),
        "industry_background": data.get("行业背景", ""),
    }


def load_stock_names() -> dict[str, str]:
    """从 stock_basic.csv 加载 ts_code -> name 映射。"""
    mapping = {}
    if not STOCK_BASIC_PATH.exists():
        return mapping
    with open(STOCK_BASIC_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_code = (row.get("ts_code") or "").strip()
            name = (row.get("name") or "").strip()
            if ts_code and name:
                mapping[ts_code] = name
    return mapping


def load_zdt_for_date(date_str: str) -> dict[str, dict]:
    """加载某日的涨跌停数据。返回 {ts_code: {limit_type, lu_desc, status, ...}}。"""
    d = date_str.replace("-", "")
    path = ZDT_DIR / f"{d}.csv"
    if not path.exists():
        return {}
    result = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_code = row.get("ts_code", "")
            if ts_code:
                result[ts_code] = {
                    "limit_type": row.get("limit_type", ""),
                    "lu_desc": row.get("lu_desc", ""),
                    "status": row.get("status", ""),
                    "pct_chg": float(row.get("pct_chg", 0) or 0),
                    "first_lu_time": row.get("first_lu_time", ""),
                    "open_num": int(float(row.get("open_num", 0) or 0)),
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


def compute_pe_percentile(pe_val: Optional[float], all_pes: list[float]) -> Optional[float]:
    """给定PE值，计算其在全体PE中的分位数。"""
    if pe_val is None or not all_pes:
        return None
    sorted_pes = sorted(all_pes)
    rank = sum(1 for p in sorted_pes if p < pe_val)
    return round(rank / len(sorted_pes) * 100, 1)


def compute_mfee_mae_ratio(results: list[dict], horizon: int) -> dict:
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
        "mfe_mean": float(np.mean(mfes)) if mfes else 0,
        "mae_mean": float(np.mean(maes)) if maes else 0,
        "ratio_mean": float(np.mean(ratios)) if ratios else 0,
        "ratio_median": float(np.median(ratios)) if ratios else 0,
    }


def _stats(arr: list[float]) -> dict:
    if not arr:
        return {"n": 0, "mean": 0, "median": 0, "std": 0, "min": 0, "max": 0}
    a = np.array(arr, dtype=float)
    return {
        "n": len(a), "mean": float(np.mean(a)), "median": float(np.median(a)),
        "std": float(np.std(a, ddof=1)), "min": float(np.min(a)), "max": float(np.max(a)),
    }


def sharpe_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    if len(returns) < 2:
        return 0.0
    r = np.array(returns, dtype=float) / 100.0
    excess = r - RISK_FREE_RATE / periods_per_year
    std_excess = np.std(excess, ddof=1)
    if std_excess == 0:
        return 0.0
    return float(np.mean(excess) / std_excess * np.sqrt(periods_per_year))


def sortino_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    if len(returns) < 2:
        return 0.0
    r = np.array(returns, dtype=float) / 100.0
    downside = r[r < 0]
    if len(downside) < 2:
        return 0.0
    target = RISK_FREE_RATE / periods_per_year
    downside_std = np.std(downside - target, ddof=1)
    if downside_std == 0:
        return 0.0
    return float(np.mean(r - target) / downside_std * np.sqrt(periods_per_year))


def calmar_ratio(returns: list[float], mdd_pct: float) -> float:
    if len(returns) < 2 or mdd_pct == 0:
        return 0.0
    r = np.array(returns, dtype=float) / 100.0
    return float(np.mean(r) * 252 / (abs(mdd_pct) / 100.0))


def max_drawdown_details(returns: list[float]) -> dict:
    if not returns:
        return {"mdd_pct": 0, "peak_idx": 0, "trough_idx": 0, "recovery_days": 0, "duration_days": 0}
    r = np.array(returns, dtype=float)
    cumulative = np.cumprod(1 + r / 100.0)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak * 100
    mdd_idx = np.argmin(drawdown)
    mdd_pct = float(drawdown[mdd_idx])
    peak_idx = np.argmax(cumulative[:mdd_idx + 1])
    recovery_idx = None
    for i in range(mdd_idx + 1, len(drawdown)):
        if drawdown[i] >= 0:
            recovery_idx = i
            break
    return {
        "mdd_pct": round(mdd_pct, 2),
        "peak_idx": int(peak_idx), "trough_idx": int(mdd_idx),
        "recovery_days": int(recovery_idx - mdd_idx) if recovery_idx else len(returns) - mdd_idx,
        "duration_days": int(mdd_idx - peak_idx),
    }


# ════════════════════════════════════════════════════════════════════════
# 图表
# ════════════════════════════════════════════════════════════════════════

def plot_equity_vs_bench(portfolio_rets, dates, bench_rets_dict, bench_dates_dict, total_ret, ann_ret, ann_vol, sr, mdd_info):
    fig, ax = plt.subplots(figsize=(16, 8))
    cum_ret = np.cumprod(1 + np.array(portfolio_rets) / 100.0)
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    ax.plot(date_objs, cum_ret, color=RED, linewidth=2.5, label="等权组合", zorder=5)
    for name, color, ls in [("创业板指", BLUE, "-"), ("中证500", ORANGE, "--"), ("沪深300", GREEN, "-.")]:
        b_rets = bench_rets_dict.get(name, [])
        b_dts = bench_dates_dict.get(name, [])
        if b_rets:
            b_cum = np.cumprod(1 + np.array(b_rets) / 100.0)
            ax.plot([datetime.strptime(d, "%Y-%m-%d") for d in b_dts], b_cum,
                    color=color, linestyle=ls, linewidth=2, label=name, alpha=0.8)
    ax.axhline(1, color="gray", linestyle=":", alpha=0.5)
    ax.set_xlabel("日期"); ax.set_ylabel("净值")
    ax.set_title("组合净值 vs 基准指数", fontsize=16, fontweight="bold")
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(AutoDateLocator())
    text = (f"总收益: {total_ret:+.2f}%\n年化: {ann_ret:+.2f}%\n波动: {ann_vol:.2f}%\n"
            f"Sharpe: {sr:.2f}\nMDD: {mdd_info['mdd_pct']:.2f}%")
    ax.text(0.02, 0.05, text, transform=ax.transAxes, fontsize=10, va="bottom",
            bbox={"boxstyle": "round,pad=0.5", "facecolor": "white", "alpha": 0.85})
    fig.autofmt_xdate()
    fig.savefig(CHARTS_DIR / "v2_01_equity.png")
    plt.close(fig)
    print("  ✓ v2_01 组合净值")


def plot_per_stock_heatmap(buy_results_sorted, horizon=20):
    """每只标的的 T+20 收益热力图（只展示有收益的标的）。"""
    n = len(buy_results_sorted)
    if n == 0:
        return
    fig, ax = plt.subplots(figsize=(6, max(6, n * 0.35)))
    rets = [r.get(f"h_{horizon}", {}).get("return_pct", 0) if r.get(f"h_{horizon}") else 0 for r in buy_results_sorted]
    # 转为二维单列
    data = np.array(rets).reshape(-1, 1)
    vmax = max(abs(min(rets)), abs(max(rets)), 1)
    cmap = sns.diverging_palette(130, 10, as_cmap=True)
    labels_y = [f"{r['symbol'] or '?'} {r['analysis_title'][:20] if r.get('analysis_title') else ''}"[:30] for r in buy_results_sorted]
    sns.heatmap(data, cmap=cmap, center=0, vmin=-vmax, vmax=vmax,
                xticklabels=[f"T+{horizon}"], yticklabels=labels_y,
                annot=True, fmt=".1f", linewidths=0.5, linecolor="white",
                cbar_kws={"label": "收益率 (%)"}, ax=ax)
    ax.set_title(f"每笔 BUY T+{horizon} 收益", fontsize=14, fontweight="bold")
    fig.savefig(CHARTS_DIR / "v2_02_per_stock_heatmap.png", bbox_inches="tight")
    plt.close(fig)
    print("  ✓ v2_02 标的收益热力图")


def plot_mfe_mae_scatter_v2(buy_results, horizon=20):
    """MFE/MAE 散点图，标注最佳/最差标的。"""
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
        symbols.append(f"{r['symbol']} {r['created_at'].strftime('%m-%d')}")
        if mfe > abs(mae) and mae > -5:
            colors_list.append(GREEN)
        elif mfe > abs(mae):
            colors_list.append(ORANGE)
        elif mae > -5:
            colors_list.append(BLUE)
        else:
            colors_list.append(RED)
    if not mfes:
        plt.close(fig); return
    max_val = max(max(abs(m) for m in mfes), max(abs(m) for m in maes)) * 1.15
    mae_abs = [abs(m) for m in maes]
    ax.axhline(0, color="black", linewidth=0.5)
    ax.axvline(0, color="black", linewidth=0.5)
    ax.plot([0, max_val], [0, max_val], color="gray", linestyle="--", alpha=0.5, label="MFE = |MAE|")
    ax.scatter(mae_abs, mfes, c=colors_list, alpha=0.7, s=80, edgecolors="white", linewidth=0.5)
    ax.text(max_val * 0.75, max_val * 0.85, "高风险高回报", fontsize=10, color=ORANGE, ha="center", alpha=0.7)
    ax.text(max_val * 0.25, max_val * 0.85, "低风险高回报 ★", fontsize=10, color=GREEN, ha="center", alpha=0.7)
    ax.text(max_val * 0.75, max_val * 0.1, "高风险低回报 ✗", fontsize=10, color=RED, ha="center", alpha=0.7)
    ax.text(max_val * 0.25, max_val * 0.1, "低风险低回报", fontsize=10, color=BLUE, ha="center", alpha=0.7)
    ax.set_xlabel(f"|MAE| (T+{horizon}, %)"); ax.set_ylabel(f"MFE (T+{horizon}, %)")
    ax.set_title(f"MFE / MAE 散点图 (T+{horizon})", fontsize=15, fontweight="bold")
    ax.set_xlim(-max_val * 0.02, max_val); ax.set_ylim(-max_val * 0.02, max_val)
    ax.set_aspect("equal"); ax.legend(loc="upper left")
    if mfes:
        best_idx = np.argmax(mfes); worst_idx = np.argmin(maes)
        ax.annotate(symbols[best_idx], (mae_abs[best_idx], mfes[best_idx]),
                    textcoords="offset points", xytext=(8, 8), fontsize=8, color=BLUE)
        ax.annotate(symbols[worst_idx], (mae_abs[worst_idx], mfes[worst_idx]),
                    textcoords="offset points", xytext=(8, -12), fontsize=8, color=RED)
    fig.savefig(CHARTS_DIR / "v2_03_mfe_mae.png")
    plt.close(fig)
    print("  ✓ v2_03 MFE/MAE 散点图")


def plot_ic_decay(ic_seq):
    hs = sorted(ic_seq.keys())
    ics = [ic_seq[h]["ic"] for h in hs]
    valid = [(h, ic) for h, ic in zip(hs, ics) if ic is not None]
    if not valid:
        return
    vh, vic = zip(*valid)
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(vh, vic, marker="o", linewidth=2.5, markersize=10, color=BLUE)
    for h, ic in zip(vh, vic):
        ax.annotate(f"{ic:.3f}", (h, ic), textcoords="offset points", xytext=(0, 12), fontsize=9, ha="center")
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.fill_between(vh, 0, vic, alpha=0.1, color=BLUE)
    ax.set_xlabel("预测周期 (交易日)"); ax.set_ylabel("Rank IC")
    ax.set_title("信息系数 (IC) 衰减曲线", fontsize=15, fontweight="bold")
    ax.set_xticks(HORIZONS)
    fig.savefig(CHARTS_DIR / "v2_05_ic_decay.png")
    plt.close(fig)
    print("  ✓ v2_05 IC衰减曲线")


def plot_valuation_vs_return(buy_results):
    """PE分位 vs T+20收益散点图（新图）。"""
    pe_vals = []
    ret_vals = []
    symbols = []
    for r in buy_results:
        pe_pct = r.get("pe_percentile")
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if pe_pct is not None and h20 is not None:
            pe_vals.append(pe_pct)
            ret_vals.append(h20)
            symbols.append(r["symbol"])
    if len(pe_vals) < 10:
        return
    fig, ax = plt.subplots(figsize=(10, 7))
    colors = [GREEN if r > 0 else RED for r in ret_vals]
    ax.scatter(pe_vals, ret_vals, c=colors, alpha=0.6, s=60, edgecolors="white")
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.axvline(50, color="gray", linestyle=":", alpha=0.3)
    ax.set_xlabel("PE 分位数 (%)"); ax.set_ylabel("T+20 收益 (%)")
    ax.set_title("PE估值分位 vs T+20收益", fontsize=15, fontweight="bold")
    # 添加回归线
    from numpy.polynomial.polynomial import polyfit
    if len(pe_vals) > 2:
        try:
            b, m = polyfit(pe_vals, ret_vals, 1)
            x_range = np.linspace(min(pe_vals), max(pe_vals), 100)
            ax.plot(x_range, b + m * x_range, color=BLUE, linestyle="--", alpha=0.7,
                    label=f"线性趋势 (y={m:.3f}x+{b:.2f})")
            ax.legend()
        except Exception:
            pass
    fig.savefig(CHARTS_DIR / "v2_13_valuation_vs_return.png")
    plt.close(fig)
    print("  ✓ v2_13 PE分位 vs 收益散点图")


def plot_risk_analysis_boxplot(buy_results, horizon=20):
    """风险等级 + 市场状态 双因子箱线图。"""
    groups: dict[str, list[float]] = defaultdict(list)
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        risk = r.get("risk_level") or "unknown"
        mkt = r.get("market_state", "unknown")
        key = f"{risk}/{mkt}"
        groups[key].append(h_data["return_pct"])
    visible_groups = {k: v for k, v in sorted(groups.items()) if len(v) >= 3}
    if not visible_groups:
        return
    fig, ax = plt.subplots(figsize=(14, 7))
    labels = list(visible_groups.keys())
    data_groups = list(visible_groups.values())
    bp = ax.boxplot(data_groups, widths=0.5, patch_artist=True,
                    medianprops={"color": "black", "linewidth": 2},
                    flierprops={"marker": "o", "markerfacecolor": RED, "alpha": 0.5})
    for patch, i in zip(bp["boxes"], range(len(data_groups))):
        patch.set_facecolor(GREEN if np.mean(data_groups[i]) > 0 else RED)
        patch.set_alpha(0.3)
    for i, data in enumerate(data_groups):
        jitter = np.random.normal(0, 0.04, len(data))
        ax.scatter(np.ones(len(data)) * (i + 1) + jitter, data, alpha=0.2, s=10, color="black")
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel(f"T+{horizon} 收益 (%)")
    ax.set_title(f"风险等级 × 市场状态 收益分布 (T+{horizon})", fontsize=15, fontweight="bold")
    fig.tight_layout()
    fig.savefig(CHARTS_DIR / "v2_14_risk_market_boxplot.png")
    plt.close(fig)
    print("  ✓ v2_14 风险×市场箱线图")


def plot_top_bottom_stocks(buy_results, horizon=20, top_n=15):
    """最赚钱/最亏钱标的排名图。"""
    by_symbol = defaultdict(list)
    for r in buy_results:
        h_data = r.get(f"h_{horizon}")
        if h_data is None:
            continue
        by_symbol[r["symbol"]].append(h_data["return_pct"])
    sym_stats = []
    for sym, rets in by_symbol.items():
        avg_r = np.mean(rets)
        if len(rets) >= 2:
            sym_stats.append((sym, len(rets), avg_r, np.std(rets, ddof=1)))
        else:
            sym_stats.append((sym, len(rets), avg_r, 0))
    sym_stats.sort(key=lambda x: x[2], reverse=True)
    display = sym_stats[:top_n] + sym_stats[-top_n:]
    names = [f"{s[0]} (n={s[1]})" for s in display]
    means = [s[2] for s in display]
    colors = [GREEN if m > 0 else RED for m in means]

    fig, ax = plt.subplots(figsize=(12, max(8, len(display) * 0.4)))
    y_pos = range(len(display))
    ax.barh(y_pos, means, color=colors, alpha=0.7, edgecolor="white")
    ax.axvline(0, color="black", linewidth=0.8)
    for i, (m, n) in enumerate(zip(means, [s[1] for s in display])):
        ax.text(m + (0.3 if m >= 0 else -0.8), i, f"{m:+.2f}% (n={n})", va="center", fontsize=8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=9)
    ax.set_xlabel(f"平均 T+{horizon} 收益 (%)")
    ax.set_title(f"最佳/最差标的排名 (T+{horizon})", fontsize=15, fontweight="bold")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(CHARTS_DIR / "v2_15_top_bottom_stocks.png")
    plt.close(fig)
    print("  ✓ v2_15 标的排名图")


def plot_stock_chart(sym, valid_buys, klines_cache, dates_cache, stock_names, out_dir):
    """单只标的走势图：K线 + 买入标记 + T+20/T+30标注。"""
    name = stock_names.get(sym, "?")
    sym_buys = [r for r in valid_buys if r["symbol"] == sym]
    if not sym_buys:
        return

    klines = klines_cache.get(sym, {})
    dates = dates_cache.get(sym, [])
    if not klines or not dates:
        return

    fig, ax = plt.subplots(figsize=(14, 7))

    # 画收盘价走势（买入前60天到买入后30天）
    all_buy_dates = [datetime.strptime(r["buy_date"], "%Y-%m-%d") for r in sym_buys]
    min_buy = min(all_buy_dates)
    max_buy = max(all_buy_dates)

    # 扩展范围
    plot_start = (min_buy - timedelta(days=120)).strftime("%Y-%m-%d")
    plot_end = (max_buy + timedelta(days=60)).strftime("%Y-%m-%d")

    plot_dates = [d for d in dates if plot_start <= d <= plot_end]
    if len(plot_dates) < 10:
        return

    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in plot_dates]
    closes = [klines[d]["close"] for d in plot_dates]

    ax.plot(date_objs, closes, color=BLUE, linewidth=1.5, alpha=0.8)

    # 标记每笔买入
    for r in sym_buys:
        buy_date_obj = datetime.strptime(r["buy_date"], "%Y-%m-%d")
        buy_price = r["buy_price"]
        # 找最近点
        for d_obj, d_str, close in zip(date_objs, plot_dates, closes):
            if d_str == r["buy_date"] or d_str >= r["buy_date"]:
                ax.scatter([d_obj], [close], color=GREEN if r.get("h_20", {}).get("return_pct", 0) > 0 else RED,
                          s=100, zorder=10, edgecolors="white", linewidth=1.5)
                break

    # 标注
    ax.set_title(f"{sym} {name} ({len(sym_buys)}笔BUY)", fontsize=14, fontweight="bold")
    ax.set_xlabel("日期"); ax.set_ylabel("收盘价")
    ax.xaxis.set_major_formatter(DateFormatter("%m-%d"))
    ax.legend(["收盘价", "买入点"], loc="upper left")
    fig.autofmt_xdate()
    safe_name = sym.replace(".", "_")
    fig.savefig(out_dir / f"{safe_name}.png")
    plt.close(fig)


# ════════════════════════════════════════════════════════════════════════
# Markdown 报告生成
# ════════════════════════════════════════════════════════════════════════

def generate_report_md(
    buys_raw, buy_results, wn_map, ns_map, fb_map, raw_map,
    stock_concepts, company_jsons, indicator_data, stock_names,
    portfolio_rets, portfolio_dates, bench_rets, bench_dates,
    ic_seq, by_symbol_stats,
):
    lines = []
    def w(*args):
        lines.append("".join(str(a) for a in args))

    valid_buys = [r for r in buy_results]
    approved = [r for r in valid_buys if r["status"] == "approved"]
    rejected = [r for r in valid_buys if r["status"] == "rejected"]
    pending = [r for r in valid_buys if r["status"] == "pending"]
    triggered = [r for r in valid_buys if r["status"] == "triggered_close"]

    # ─── 头部 ───
    w("# 回测评估报告 v2 — BUY 操作深度分析")
    w()
    w(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    w(f"**数据来源**: quant_kb (PostgreSQL: localhost:15432) + C:/klines/ (日线/指标/概念/涨跌停/公司档案)")
    w(f"**回测区间**: 2026-03-24 ~ 2026-06-26 (~3个月)")
    w(f"**分析范围**: 仅 BUY 操作")
    w()

    # ─── 目录 ───
    w("## 目录")
    w()
    sections = [
        ("0. 数据全貌", "0-数据全貌"),
        ("A. 组合层面指标", "a-组合层面指标"),
    ]
    for title, anchor in sections:
        w(f"- [{title}](#{anchor})")
    w(f"- [B. 个股级深度分析](#b-个股级深度分析)")
    w(f"  - [Top 20 / Bottom 20 排名](#top-20-最佳标的-按-t20-平均收益)")
    w(f"  - [个股详细分析卡（{len(by_symbol_stats)}只）](#个股详细分析卡)")
    for idx in range(len(by_symbol_stats)):
        sym, cnt, avg20, std20, avg30, mfe20, mae20, ratio20, pe_pct, concepts, market_states, first_date = by_symbol_stats[idx]
        name = stock_names.get(sym, "?")
        anchor = f"{idx+1:03d}-{sym}-{name}"
        w(f"    - [{idx+1}. {sym} {name}](#{anchor})")
    remaining_sections = [
        ("C. 决策链路深度追踪", "c-决策链路深度追踪"),
        ("D. 多维交叉分析矩阵", "d-多维交叉分析矩阵"),
        ("E. 信号时间分析", "e-信号时间分析"),
        ("F. 尾部风险分析", "f-尾部风险分析"),
        ("图表索引", "图表索引"),
    ]
    for title, anchor in remaining_sections:
        w(f"- [{title}](#{anchor})")
    w()

    # ─── 0. 数据全貌 ───
    w("---")
    w("## 0. 数据全貌")
    w()
    total_buys = len(buys_raw)
    valid_buys_cnt = len(valid_buys)
    unique_symbols = len(set(r["symbol"] for r in valid_buys))

    has_analysis = sum(1 for r in buys_raw if r["analysis_id"])
    has_node = sum(1 for r in buys_raw if r["target_node_id"] and r["target_node_id"] in wn_map)
    has_fb = sum(1 for r in buys_raw if r["id"] in fb_map)
    has_raw = sum(1 for r in buys_raw if r.get("root_raw_info_ids"))
    has_indicator = sum(1 for r in valid_buys if r.get("pe_percentile") is not None)
    has_concept = sum(1 for r in valid_buys if r.get("concepts"))
    has_zdt = sum(1 for r in valid_buys if r.get("zdt_info"))

    w("### 数据库关联覆盖率")
    w()
    w(f"| 维度 | 覆盖数 | 覆盖率 | 说明 |")
    w(f"|------|--------|--------|------|")
    w(f"| 总 BUY 操作 | {total_buys} | 100% | - |")
    w(f"| 有效（有日线数据） | {valid_buys_cnt} | {valid_buys_cnt/total_buys*100:.1f}% | 过滤掉数据不足的记录 |")
    w(f"| → analyses 关联 | {has_analysis} | {has_analysis/total_buys*100:.1f}% | trigger_analysis_id JOIN |")
    w(f"| → world_nodes 关联 | {has_node} | {has_node/total_buys*100:.1f}% | target_node_id JOIN |")
    w(f"| → node_states 关联 | {sum(1 for r in buys_raw if r.get('target_node_id') in ns_map)} | {sum(1 for r in buys_raw if r.get('target_node_id') in ns_map)/total_buys*100:.1f}% | 核心逻辑/风险/驱动因素 |")
    w(f"| → feedbacks 关联 | {has_fb} | {has_fb/total_buys*100:.1f}% | 事后评价反馈 |")
    w(f"| → raw_information 可追溯 | {has_raw} | {has_raw/total_buys*100:.1f}% | 可回溯原始新闻 |")
    w(f"| → PE/估值数据 | {has_indicator} | {has_indicator/max(1,valid_buys_cnt)*100:.1f}% | 从 indicator/ 获取 |")
    w(f"| → 概念/板块归属 | {has_concept} | {has_concept/max(1,valid_buys_cnt)*100:.1f}% | stock_concepts.csv |")
    w(f"| → 涨跌停数据 | {has_zdt} | {has_zdt/max(1,valid_buys_cnt)*100:.1f}% | 买入日是否涨停 |")
    w()

    w("### BUY 操作分布")
    w()
    w(f"| 维度 | 分布 |")
    w(f"|------|------|")
    w(f"| 总 BUY | {total_buys} 条 |")
    w(f"| 有效 BUY (有日线) | {valid_buys_cnt} 条 |")
    w(f"| 不同标的 | {unique_symbols} 只 |")
    w(f"| 时间范围 | {min(r['created_at'] for r in buys_raw)} ~ {max(r['created_at'] for r in buys_raw)} |")
    w()
    # status 分布
    w("#### 按 status 分布")
    w(f"| Status | 数量 | 占比 |")
    w(f"|--------|------|------|")
    for label, group in [("approved", approved), ("rejected", rejected), ("pending", pending), ("triggered_close", triggered)]:
        w(f"| {label} | {len(group)} | {len(group)/max(1,valid_buys_cnt)*100:.1f}% |")
    w()

    # risk_level 分布
    w("#### 按 risk_level 分布")
    w(f"| Risk Level | 数量 | 占比 |")
    w(f"|------------|------|------|")
    for rl in ["low", "medium", "high", "critical"]:
        cnt = sum(1 for r in valid_buys if (r.get("risk_level") or "") == rl)
        if cnt > 0:
            w(f"| {rl} | {cnt} | {cnt/max(1,valid_buys_cnt)*100:.1f}% |")
    w()

    # analysis_type 分布
    w("#### 按 analysis_type × time_horizon 分布")
    at_map = defaultdict(lambda: defaultdict(int))
    for r in valid_buys:
        at = r.get("analysis_type") or "unknown"
        th = r.get("time_horizon") or "unknown"
        at_map[at][th] += 1
    w(f"| Analysis Type | Time Horizon | 数量 |")
    w(f"|---------------|--------------|------|")
    for at in sorted(at_map):
        for th in sorted(at_map[at]):
            w(f"| {at} | {th} | {at_map[at][th]} |")
    w()

    # ─── A. 组合层面 ───
    w("---")
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
        wr = sum(1 for x in portfolio_rets if x > 0) / len(portfolio_rets) * 100

        w(f"| 指标 | 数值 | 说明 |")
        w(f"|------|------|------|")
        w(f"| 总收益 | {total_ret:+.2f}% | 组合累计 |")
        w(f"| 年化收益率 | {ann_ret:+.2f}% | - |")
        w(f"| 年化波动率 | {ann_vol:.2f}% | - |")
        w(f"| **Sharpe Ratio** | **{sr:.3f}** | 风险调整收益 |")
        w(f"| **Sortino Ratio** | **{sor:.3f}** | 仅惩罚下行 |")
        w(f"| **Calmar Ratio** | **{cal:.3f}** | 收益/最大回撤 |")
        w(f"| 最大回撤 | {mdd_info['mdd_pct']:.2f}% | 持续 {mdd_info['duration_days']} 天 |")
        w(f"| 日胜率 | {wr:.1f}% | - |")
        w(f"| VaR(95%) | {np.percentile(portfolio_rets, 5):.2f}% | 日度 |")
        w(f"| VaR(99%) | {np.percentile(portfolio_rets, 1):.2f}% | 日度 |")
        w(f"| CVaR(95%) | {np.mean([x for x in portfolio_rets if x <= np.percentile(portfolio_rets, 5)]):.2f}% | 条件风险价值 |")
        w(f"| 偏度 | {float(pd.Series(portfolio_rets).skew()):.2f} | 收益分布不对称 |")
        w(f"| 峰度 | {float(pd.Series(portfolio_rets).kurtosis()):.2f} | 尾部风险 |")

        w()
        w("### 基准对比")
        w()
        w(f"| 基准 | 总收益 | 年化收益 | 年化波动 | Sharpe | MDD |")
        w(f"|------|--------|----------|----------|--------|-----|")
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
        w(f"![组合净值](charts/v2_01_equity.png)")
    w()

    # ─── B. 个股级分析 ───
    w("---")
    w("## B. 个股级深度分析")
    w()
    w(f"以下对 {len(by_symbol_stats)} 只标的进行逐个分析。")
    w()

    # 收益分布概览
    all_ret20 = [r.get("h_20", {}).get("return_pct") for r in valid_buys if r.get("h_20")]
    all_ret20 = [x for x in all_ret20 if x is not None]
    if all_ret20:
        st = _stats(all_ret20)
        pos = sum(1 for x in all_ret20 if x > 0)
        w("### 所有 BUY 的 T+20 收益分布")
        w(f"| 指标 | 数值 |")
        w(f"|------|------|")
        w(f"| 样本数 | {st['n']} |")
        w(f"| 均值 | {st['mean']:+.2f}% |")
        w(f"| 中位数 | {st['median']:+.2f}% |")
        w(f"| 标准差 | {st['std']:.2f}% |")
        w(f"| 胜率 | {pos/st['n']*100:.1f}% ({pos}/{st['n']}) |")
        w(f"| 最佳 | {st['max']:+.2f}% |")
        w(f"| 最差 | {st['min']:+.2f}% |")
        w()

    # Top 20 最佳标的
    w("### Top 20 最佳标的 (按 T+20 平均收益)")
    w()
    w(f"| 排名 | 标的/名称 | 日期 | 次数 | T+20均值 | T+30均值 | MFE均值 | MAE均值 | MFE/MAE | PE分位 | 主要概念 |")
    w(f"|------|-----------|------|------|----------|----------|---------|---------|---------|--------|----------|")
    for i, (sym, cnt, avg20, std20, avg30, mfe20, mae20, ratio20, pe_pct, concepts, market_states, first_date) in enumerate(by_symbol_stats[:20], 1):
        name = stock_names.get(sym, "?")
        concepts_str = ", ".join(concepts[:3]) if concepts else "-"
        w(f"| {i} | {sym} {name} | {first_date} | {cnt} | {avg20:+.2f}% | {avg30:+.2f}% | {mfe20:+.2f}% | {mae20:+.2f}% | {ratio20:.2f} | {pe_pct or '-'} | {concepts_str} |")
    w()

    # Bottom 20
    w("### Bottom 20 最差标的 (按 T+20 平均收益)")
    w()
    w(f"| 排名 | 标的/名称 | 日期 | 次数 | T+20均值 | T+30均值 | MFE均值 | MAE均值 | MFE/MAE | PE分位 | 主要概念 |")
    w(f"|------|-----------|------|------|----------|----------|---------|---------|---------|--------|----------|")
    for i, (sym, cnt, avg20, std20, avg30, mfe20, mae20, ratio20, pe_pct, concepts, market_states, first_date) in enumerate(by_symbol_stats[-20:], 1):
        name = stock_names.get(sym, "?")
        concepts_str = ", ".join(concepts[:3]) if concepts else "-"
        w(f"| {i} | {sym} {name} | {first_date} | {cnt} | {avg20:+.2f}% | {avg30:+.2f}% | {mfe20:+.2f}% | {mae20:+.2f}% | {ratio20:.2f} | {pe_pct or '-'} | {concepts_str} |")
    w()
    w(f"![标的排名](charts/v2_15_top_bottom_stocks.png)")
    w()

    # 每只标的的详细分析卡（全部135只）
    w("### 个股详细分析卡")
    w()
    for idx in range(len(by_symbol_stats)):
        sym, cnt, avg20, std20, avg30, mfe20, mae20, ratio20, pe_pct, concepts, market_states, first_date = by_symbol_stats[idx]
        w(f"#### {idx + 1}. {sym} {stock_names.get(sym, '?')}")
        w()
        # 基本信息
        comp_info = company_jsons.get(sym, {})
        w(f"| 属性 | 内容 |")
        w(f"|------|------|")
        w(f"| **标的** | {sym} {stock_names.get(sym, '?')} |")
        w(f"| **首次买入** | {first_date} |")
        w(f"| **BUY 次数** | {cnt} |")
        w(f"| **T+20 平均收益** | **{avg20:+.2f}%** (std={std20:.2f}%) |")
        w(f"| **T+30 平均收益** | {avg30:+.2f}% |")
        w(f"| **MFE/MAE 比率** | {ratio20:.2f} |")
        if pe_pct is not None:
            w(f"| **PE 分位** | {pe_pct}% |")
        if concepts:
            w(f"| **主要概念** | {', '.join(concepts[:5])} |")
        if market_states:
            w(f"| **市场状态** | {', '.join(market_states)} |")
        if comp_info.get("industry_dongcai"):
            w(f"| **东财行业** | {comp_info['industry_dongcai']} |")
        if comp_info.get("main_business"):
            w(f"| **主营业务** | {comp_info['main_business'][:100]} |")
        if comp_info.get("core_competitiveness"):
            w(f"| **核心竞争力** | {comp_info['core_competitiveness'][:100]} |")

        # 关联的 analysis 标题 (from valid_buys)
        sym_buys = [r for r in valid_buys if r["symbol"] == sym]
        if sym_buys and sym_buys[0].get("analysis_title"):
            w(f"| **分析标题** | {sym_buys[0]['analysis_title'][:100]} |")

        # 关联的 node_state 核心逻辑
        for r in sym_buys:
            nid = r.get("target_node_id")
            if nid and nid in ns_map:
                ns = ns_map[nid]
                if ns.get("core_logic"):
                    w(f"| **核心逻辑** | {ns['core_logic'][:150]}... |")
                break

        # feedbacks (use original_id key)
        for r in sym_buys:
            tid = r.get("original_id")
            if tid and tid in fb_map:
                for fb in fb_map[tid]:
                    if fb.get("lessons_learned"):
                        w(f"| **教训** | {fb['lessons_learned'][:150]}... |")
                        break
                break

        # 嵌入个股走势图
        safe_sym = sym.replace(".", "_")
        w(f"![{sym}走势图](charts/stock_charts/{safe_sym}.png)")
        w()
        w()

    w(f"![标的热力图](charts/v2_02_per_stock_heatmap.png)")
    w()

    # ─── C. 决策链路追踪 ───
    w("---")
    w("## C. 决策链路深度追踪")
    w()

    # C1. analysis_type 表现
    w("### C1. Analysis Type × 收益表现 (T+20)")
    w()
    w(f"| Analysis Type | Time Horizon | N | T+20均值 | 胜率 | MFE均值 | MAE均值 |")
    w(f"|---------------|--------------|---|----------|------|---------|---------|")
    by_at_th: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in valid_buys:
        at = r.get("analysis_type") or "unknown"
        th = r.get("time_horizon") or "unknown"
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if h20 is not None:
            by_at_th[at][th].append(h20)
    for at in sorted(by_at_th):
        for th in sorted(by_at_th[at]):
            vals = by_at_th[at][th]
            st_vals = _stats(vals)
            wr = sum(1 for x in vals if x > 0) / len(vals) * 100
            mm = compute_mfee_mae_ratio([r for r in valid_buys if (r.get("analysis_type") or "unknown") == at and (r.get("time_horizon") or "unknown") == th], 20)
            w(f"| {at} | {th} | {len(vals)} | {st_vals['mean']:+.2f}% | {wr:.1f}% | {mm['mfe_mean']:+.2f}% | {mm['mae_mean']:+.2f}% |")
    w()

    # C2. confidence 单调性（增强版）
    w("### C2. Confidence 信号强度分析")
    w()
    scored = [(r.get("confidence"), r.get("h_20", {}).get("return_pct") if r.get("h_20") else None)
              for r in valid_buys if r.get("confidence") is not None and r.get("h_20") is not None]
    scored = [(c, r) for c, r in scored if r is not None]
    if len(scored) >= 15:
        scored.sort(key=lambda x: x[0])
        n = len(scored)
        n_groups = min(5, n // 3)
        gs = n // n_groups
        w(f"| 分组 | 信心区间 | N | T+20均值 | 胜率 | T+30均值 |")
        w(f"|------|----------|---|----------|------|----------|")
        for g in range(n_groups):
            start = g * gs
            end = start + gs if g < n_groups - 1 else n
            group = scored[start:end]
            conf_range = f"{group[0][0]:.2f}-{group[-1][0]:.2f}"
            rets20 = [x[1] for x in group]
            rets30 = [r.get("h_30", {}).get("return_pct") for r in valid_buys
                      if r.get("confidence") and group[0][0] <= r["confidence"] <= group[-1][0]
                      and r.get("h_30")]
            rets30 = [x for x in rets30 if x is not None]
            avg20 = np.mean(rets20)
            wr20 = sum(1 for x in rets20 if x > 0) / len(rets20) * 100
            avg30 = np.mean(rets30) if rets30 else 0
            w(f"| G{g + 1} | {conf_range} | {len(group)} | {avg20:+.2f}% | {wr20:.1f}% | {avg30:+.2f}% |")
        from scipy.stats import spearmanr
        group_means = [np.mean([x[1] for x in scored[g*gs:(g+1)*gs if g < n_groups-1 else n]]) for g in range(n_groups)]
        rho, p = spearmanr(range(1, n_groups + 1), group_means)
        w(f"\n**Spearman ρ = {rho:.3f} (p = {p:.3f})** — {'正向单调 ✓ 信号有效' if rho > 0.3 and p < 0.1 else '无显著单调性'}")
    w()
    w(f"![IC衰减](charts/v2_05_ic_decay.png)")
    w()

    # C3. feedbacks 事后评价
    w("### C3. 事后反馈 (Feedbacks) 分析")
    w()
    fb_buys = [r for r in buys_raw if r["id"] in fb_map]
    if fb_buys:
        correct = sum(1 for r in fb_buys for fb in fb_map[r["id"]] if fb["judgment_correct"])
        wrong = sum(1 for r in fb_buys for fb in fb_map[r["id"]] if fb["judgment_correct"] == False)
        total_fb = correct + wrong
        w(f"**有反馈的 BUY**: {len(fb_buys)}/{total_buys} ({len(fb_buys)/total_buys*100:.1f}%)")
        w(f"**判断正确率**: {correct}/{total_fb} ({correct/total_fb*100:.1f}%)")
        w()

        # 常见错误原因（前10）
        errors = []
        for r in fb_buys:
            for fb in fb_map[r["id"]]:
                if fb["error_reason"]:
                    errors.append(fb["error_reason"])
        if errors:
            w("#### 常见错误原因汇总")
            w()
            for i, err in enumerate(errors[:10], 1):
                w(f"{i}. {err[:200]}")
            w()

        # 关键教训
        lessons = []
        for r in fb_buys:
            for fb in fb_map[r["id"]]:
                if fb["lessons_learned"]:
                    lessons.append(fb["lessons_learned"])
        if lessons:
            w("#### 关键教训汇总")
            w()
            for i, lesson in enumerate(lessons[:10], 1):
                w(f"{i}. {lesson[:200]}")
            w()
    w()

    # ─── D. 多维交叉分析 ───
    w("---")
    w("## D. 多维交叉分析矩阵")
    w()

    # D1. 风险×市场
    w("### D1. Risk Level × Market State (T+20)")
    w()
    w(f"| Risk Level | Market | N | T+20均值 | T+20中位数 | 胜率 | MFE/MAE |")
    w(f"|------------|--------|---|----------|-----------|------|---------|")
    cross: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in valid_buys:
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if h20 is None:
            continue
        risk = r.get("risk_level") or "unknown"
        mkt = r.get("market_state", "unknown")
        cross[risk][mkt].append(h20)
    for risk in ["low", "medium", "high", "critical", "unknown"]:
        for mkt in ["bull", "range", "bear", "unknown"]:
            vals = cross[risk].get(mkt, [])
            if len(vals) >= 2:
                st_vals = _stats(vals)
                wr = sum(1 for x in vals if x > 0) / len(vals) * 100
                # MFE/MAE
                relevant = [r for r in valid_buys if (r.get("risk_level") or "unknown") == risk and r.get("market_state") == mkt and r.get("h_20")]
                mm = compute_mfee_mae_ratio(relevant, 20)
                w(f"| {risk} | {mkt} | {len(vals)} | {st_vals['mean']:+.2f}% | {st_vals['median']:+.2f}% | {wr:.1f}% | {mm['ratio_mean']:.2f} |")
    w()
    w(f"![风险×市场](charts/v2_14_risk_market_boxplot.png)")
    w()

    # D2. 估值分层
    w("### D2. PE 估值分位 × T+20 收益")
    w()
    pe_groups = defaultdict(list)
    for r in valid_buys:
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if h20 is None or r.get("pe_percentile") is None:
            continue
        pct = r["pe_percentile"]
        if pct <= 20:
            pe_groups["低PE (0-20%)"].append(h20)
        elif pct <= 40:
            pe_groups["中低PE (20-40%)"].append(h20)
        elif pct <= 60:
            pe_groups["中PE (40-60%)"].append(h20)
        elif pct <= 80:
            pe_groups["中高PE (60-80%)"].append(h20)
        else:
            pe_groups["高PE (80-100%)"].append(h20)
    w(f"| PE分位 | N | T+20均值 | 中位数 | 胜率 |")
    w(f"|--------|---|----------|--------|------|")
    for label in ["低PE (0-20%)", "中低PE (20-40%)", "中PE (40-60%)", "中高PE (60-80%)", "高PE (80-100%)"]:
        vals = pe_groups.get(label, [])
        if vals:
            st_vals = _stats(vals)
            wr = sum(1 for x in vals if x > 0) / len(vals) * 100
            w(f"| {label} | {len(vals)} | {st_vals['mean']:+.2f}% | {st_vals['median']:+.2f}% | {wr:.1f}% |")
    w()
    w(f"![PE vs 收益](charts/v2_13_valuation_vs_return.png)")
    w()

    # D3. 概念热度
    w("### D3. 概念热度 × 收益")
    w()
    concept_stats = defaultdict(list)
    for r in valid_buys:
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if h20 is None:
            continue
        for c in r.get("concepts", []):
            concept_stats[c].append(h20)
    # 按热度分组
    hot = {k: v for k, v in concept_stats.items() if len(v) >= 10}
    if hot:
        sorted_concepts = sorted(hot.items(), key=lambda x: np.mean(x[1]), reverse=True)
        w(f"| 概念 | 交易次数 | T+20均值 | 胜率 |")
        w(f"|------|----------|----------|------|")
        for c, rets in sorted_concepts[:15]:
            avg = np.mean(rets)
            wr = sum(1 for x in rets if x > 0) / len(rets) * 100
            w(f"| {c} | {len(rets)} | {avg:+.2f}% | {wr:.1f}% |")
        w()
        w(f"| ... | ... | ... | ... |")
        w(f"|------|----------|----------|------|")
        for c, rets in sorted_concepts[-10:]:
            avg = np.mean(rets)
            wr = sum(1 for x in rets if x > 0) / len(rets) * 100
            w(f"| {c} | {len(rets)} | {avg:+.2f}% | {wr:.1f}% |")
    w()

    # D4. 涨停日分析
    w("### D4. 涨停日买入分析")
    w()
    zdt_buys = [r for r in valid_buys if r.get("zdt_info")]
    non_zdt = [r for r in valid_buys if not r.get("zdt_info")]
    if zdt_buys:
        zdt_ret20 = [r["h_20"]["return_pct"] for r in zdt_buys if r.get("h_20")]
        non_ret20 = [r["h_20"]["return_pct"] for r in non_zdt if r.get("h_20")]
        zdt_st = _stats(zdt_ret20) if zdt_ret20 else _stats([])
        non_st = _stats(non_ret20) if non_ret20 else _stats([])
        w(f"| 分组 | N | T+20均值 | 胜率 | T+5均值 | T+10均值 |")
        w(f"|------|---|----------|------|----------|----------|")
        zdt_ret5 = [r["h_5"]["return_pct"] for r in zdt_buys if r.get("h_5")]
        non_ret5 = [r["h_5"]["return_pct"] for r in non_zdt if r.get("h_5")]
        zdt_ret10 = [r["h_10"]["return_pct"] for r in zdt_buys if r.get("h_10")]
        non_ret10 = [r["h_10"]["return_pct"] for r in non_zdt if r.get("h_10")]
        wr_zdt = sum(1 for x in zdt_ret20 if x > 0) / len(zdt_ret20) * 100 if zdt_ret20 else 0
        wr_non = sum(1 for x in non_ret20 if x > 0) / len(non_ret20) * 100 if non_ret20 else 0
        w(f"| 涨停日买入 | {len(zdt_buys)} | {np.mean(zdt_ret20):+.2f}% | {wr_zdt:.1f}% | {np.mean(zdt_ret5):+.2f}% | {np.mean(zdt_ret10):+.2f}% |")
        w(f"| 非涨停日 | {len(non_zdt)} | {np.mean(non_ret20):+.2f}% | {wr_non:.1f}% | {np.mean(non_ret5):+.2f}% | {np.mean(non_ret10):+.2f}% |")
    w()

    # D5. 多次推荐标的
    w("### D5. 多次推荐标的分析")
    w()
    multi_buy = [(sym, cnt) for sym, cnt in [(s, sum(1 for r in valid_buys if r["symbol"] == s)) for s in set(r["symbol"] for r in valid_buys)] if cnt >= 3]
    multi_buy.sort(key=lambda x: x[1], reverse=True)
    if multi_buy:
        w(f"| 标的 | 推荐次数 | T+20均值 | 收益标准差 | 稳定性 |")
        w(f"|------|----------|----------|-----------|--------|")
        for sym, cnt in multi_buy[:10]:
            rets = [r["h_20"]["return_pct"] for r in valid_buys if r["symbol"] == sym and r.get("h_20")]
            avg_r = np.mean(rets) if rets else 0
            std_r = np.std(rets, ddof=1) if len(rets) > 1 else 0
            stability = "稳定" if std_r < 10 else ("波动" if std_r < 20 else "剧烈波动")
            w(f"| {sym} | {cnt} | {avg_r:+.2f}% | {std_r:.2f}% | {stability} |")
    w()

    # ─── E. 信号时间分析 ───
    w("---")
    w("## E. 信号时间分析")
    w()
    hour_dist = defaultdict(int)
    for r in buys_raw:
        h = r["created_at"].hour if hasattr(r["created_at"], "hour") else 0
        hour_dist[h] += 1
    w("### 信号小时分布")
    w()
    w(f"| 小时 | 信号数 |")
    w(f"|------|--------|")
    for h in sorted(hour_dist):
        w(f"| {h}:00 | {hour_dist[h]} |")
    w()

    # 按周
    weekly = defaultdict(list)
    for r in valid_buys:
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if h20 is None:
            continue
        wk = r["created_at"].strftime("%Y-W%W")
        weekly[wk].append(h20)
    w("### 按周统计")
    w()
    w(f"| 周 | N | T+20均值 | 胜率 |")
    w(f"|----|---|----------|------|")
    for wk in sorted(weekly):
        vals = weekly[wk]
        avg = np.mean(vals)
        wr = sum(1 for x in vals if x > 0) / len(vals) * 100
        w(f"| {wk} | {len(vals)} | {avg:+.2f}% | {wr:.1f}% |")
    w()

    # ─── F. 尾部风险 ───
    w("---")
    w("## F. 尾部风险分析")
    w()
    all_ret20 = [r.get("h_20", {}).get("return_pct") for r in valid_buys if r.get("h_20")]
    all_ret20 = [x for x in all_ret20 if x is not None]
    if all_ret20:
        worst_10pct = sorted(all_ret20)[:max(1, len(all_ret20) // 10)]
        best_10pct = sorted(all_ret20, reverse=True)[:max(1, len(all_ret20) // 10)]
        w(f"| 指标 | 数值 |")
        w(f"|------|------|")
        w(f"| 最差10%均值 | {np.mean(worst_10pct):.2f}% |")
        w(f"| 最佳10%均值 | {np.mean(best_10pct):.2f}% |")
        w(f"| 尾部比率(最佳/最差) | {abs(np.mean(best_10pct)/np.mean(worst_10pct)):.2f}x |")
        w(f"| 赢亏比(正收益均值/负收益均值) | {abs(np.mean([x for x in all_ret20 if x>0])/np.mean([x for x in all_ret20 if x<0])):.2f}x" if [x for x in all_ret20 if x < 0] else "| 赢亏比 | N/A |")

        w()
        w("### 最大回撤超过 20% 的标的")
        w()
        w(f"| 标的 | 日期 | T+20收益 | 最大回撤 |")
        w(f"|------|------|----------|----------|")
        for r in valid_buys:
            h20 = r.get("h_20")
            if h20 and h20.get("mfe_pct", 0) < -20:
                w(f"| {r['symbol']} | {r['created_at'].strftime('%Y-%m-%d')} | {h20['return_pct']:.2f}% | {h20['mae_pct']:.2f}% |")
    w()

    # ─── 图表索引 ───
    w("---")
    w("## 图表索引")
    w()
    for desc in [
        "v2_01: 组合净值 vs 基准", "v2_02: 每笔BUY T+20收益热力图",
        "v2_03: MFE/MAE散点图", "v2_05: IC衰减曲线",
        "v2_13: PE估值分位 vs T+20收益", "v2_14: 风险×市场状态箱线图",
        "v2_15: 最佳/最差标的排名",
    ]:
        w(f"- {desc}")
    w()
    w(f"*报告生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  Markdown 报告已保存: {REPORT_PATH}")


# ════════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════════

import pandas as pd

bench_data: dict[str, dict[str, dict]] = {}
bench_dates: dict[str, list[str]] = {}
bench_rets: dict[str, list[float]] = {}
bench_dts: dict[str, list[str]] = {}


def main():
    global bench_data, bench_dates, bench_rets, bench_dts

    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, dbname=DB_NAME,
    )
    conn.set_client_encoding("UTF8")

    # ── 1. 加载数据库数据 ──
    print("加载数据库数据...")
    buys_raw, wn_map, ns_map, fb_map, raw_map = fetch_all_data(conn)
    conn.close()
    print(f"  BUY 操作: {len(buys_raw)} 条")
    print(f"  World Nodes: {len(wn_map)}")
    print(f"  Node States: {len(ns_map)}")
    print(f"  Feedbacks: {sum(len(v) for v in fb_map.values())} 条关联")
    print(f"  Raw Info: {len(raw_map)}")

    # ── 2. 加载本地数据 ──
    print("\n加载本地数据...")
    symbols_needed = sorted(set(b["symbol"] for b in buys_raw if b["symbol"]))
    print(f"  需要加载 {len(symbols_needed)} 只标的日线/指标...")

    # 日线
    klines_cache = {}
    dates_cache = {}
    for i, sym in enumerate(symbols_needed):
        klines_cache[sym] = load_klines(sym)
        dates_cache[sym] = _sorted_dates(klines_cache[sym])
        if (i + 1) % 50 == 0:
            print(f"    日线: {i + 1}/{len(symbols_needed)}")
    print(f"  日线: {len(symbols_needed)}/{len(symbols_needed)}")

    # 基准指数
    for name, _ in BENCHMARKS:
        bench_data[name] = load_klines(name)
        bench_dates[name] = _sorted_dates(bench_data[name])
    print(f"  基准指数: {', '.join(bench_data.keys())}")

    # 指标
    indicator_data = {}
    all_pe_values = []
    for i, sym in enumerate(symbols_needed):
        indicator_data[sym] = load_indicator(sym)
        for d, vals in indicator_data[sym].items():
            if vals.get("pe"):
                all_pe_values.append(vals["pe"])
        if (i + 1) % 100 == 0:
            print(f"    指标: {i + 1}/{len(symbols_needed)}")
    print(f"  指标: {len(symbols_needed)}/{len(symbols_needed)}, PE样本: {len(all_pe_values)}")

    # 概念
    stock_concepts = load_stock_concepts()
    print(f"  概念映射: {len(stock_concepts)} 只标的")

    # 公司档案
    company_jsons = {}
    for sym in symbols_needed:
        cj = load_company_json(sym)
        if cj:
            company_jsons[sym] = cj
    print(f"  公司档案: {len(company_jsons)}/{len(symbols_needed)}")

    # 股票名称映射
    stock_names = load_stock_names()
    print(f"  股票名称: {len(stock_names)} 条")

    # ── 3. 计算每笔 BUY 的收益指标 ──
    print("\n计算每笔 BUY 的收益指标...")
    valid_buys = []
    for b in buys_raw:
        sym = b["symbol"]
        if not sym:
            continue
        klines = klines_cache.get(sym, {})
        if not klines:
            continue
        effective_date = _effective_buy_date(b["created_at"])
        dates = dates_cache[sym]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue
        if len(dates) - idx <= max(HORIZONS):
            continue
        rets = get_next_n_returns(dates, klines, idx, close)
        # 市场状态
        cy_klines = bench_data.get("创业板指", {})
        cy_dates = bench_dates.get("创业板指", [])
        market_state = classify_market_state(cy_klines, cy_dates, found_date) if cy_klines else "unknown"
        # 估值分位
        indicator = indicator_data.get(sym, {})
        ind_date = indicator.get(found_date) or indicator.get(effective_date)
        pe_val = ind_date.get("pe") if ind_date else None
        pe_pct = compute_pe_percentile(pe_val, all_pe_values) if pe_val and all_pe_values else None
        # 概念
        concepts = stock_concepts.get(sym, [])
        # 涨停
        zdt_data = load_zdt_for_date(effective_date) if sym else {}
        zdt_info = zdt_data.get(sym)
        # 公司信息
        comp_info = company_jsons.get(sym, {})

        entry = {
            "symbol": sym, "created_at": b["created_at"], "status": b["status"],
            "risk_level": b.get("risk_level") or "unknown",
            "buy_price": close, "buy_date": found_date,
            "effective_buy_date": effective_date, "market_state": market_state,
            "pe_percentile": pe_pct, "pe_value": pe_val,
            "pb_value": ind_date.get("pb") if ind_date else None,
            "total_mv": ind_date.get("total_mv") if ind_date else None,
            "circ_mv": ind_date.get("circ_mv") if ind_date else None,
            "concepts": concepts,
            "zdt_info": zdt_info,
            "company_info": comp_info,
            "analysis_id": b["analysis_id"],
            "analysis_title": b["analysis_title"],
            "analysis_content": b["analysis_content"],
            "confidence": b["confidence"],
            "time_horizon": b["time_horizon"],
            "analysis_type": b["analysis_type"],
            "target_node_id": b["target_node_id"],
            "original_id": b["id"],
            "root_raw_info_ids": b.get("root_raw_info_ids", []),
        }
        for h in HORIZONS:
            entry[f"h_{h}"] = rets.get(h)
        valid_buys.append(entry)

    print(f"  有效 BUY: {len(valid_buys)}/{len(buys_raw)} (过滤 {len(buys_raw) - len(valid_buys)} 条)")

    # ── 4. 组合层面指标 ──
    print("\n计算组合日收益...")
    MAX_H = max(HORIZONS)
    daily_contributions = defaultdict(list)
    for r in valid_buys:
        sym = r["symbol"]
        klines = klines_cache.get(sym, {})
        dates = dates_cache.get(sym, [])
        _, _, idx = get_close_on_or_after(dates, klines, r["buy_date"])
        if idx is None:
            continue
        for j in range(idx, min(idx + MAX_H, len(dates) - 1)):
            d = dates[j]
            next_d = dates[j + 1]
            day_ret = (klines[next_d]["close"] - klines[d]["close"]) / klines[d]["close"] * 100
            daily_contributions[d].append(day_ret)
    all_dates = sorted(daily_contributions.keys())
    portfolio_rets = [np.mean(daily_contributions[d]) for d in all_dates]

    bench_rets = {}
    bench_dts = {}
    for name, _ in BENCHMARKS:
        b_klines = bench_data.get(name, {})
        if not b_klines:
            continue
        b_ret_list, b_date_list = [], []
        for d in all_dates:
            if d in b_klines:
                b_ret_list.append(b_klines[d]["pct_chg"])
                b_date_list.append(d)
        bench_rets[name] = b_ret_list
        bench_dts[name] = b_date_list
    print(f"  组合交易日: {len(portfolio_rets)}")

    # ── 5. IC 分析 ──
    print("\n计算 IC 序列...")
    scores = []
    rets_by_h = defaultdict(list)
    for r in valid_buys:
        conf = r.get("confidence")
        if conf is None:
            continue
        scores.append(conf)
        for h in HORIZONS:
            h_data = r.get(f"h_{h}")
            rets_by_h[h].append(h_data["return_pct"] if h_data else None)
    scores_arr = np.array(scores, dtype=float)
    from scipy.stats import spearmanr
    ic_seq = {}
    for h in HORIZONS:
        rets_arr = np.array(rets_by_h[h], dtype=float)
        mask = ~np.isnan(scores_arr) & ~np.isnan(rets_arr)
        ic = None
        if mask.sum() >= 10:
            corr, _ = spearmanr(scores_arr[mask], rets_arr[mask])
            ic = float(corr)
        ic_seq[h] = {"ic": ic, "n": mask.sum()}

    # ── 6. 按标的汇总 ──
    print("\n按标的汇总...")
    by_symbol_raw = defaultdict(list)
    for r in valid_buys:
        by_symbol_raw[r["symbol"]].append(r)
    by_symbol_stats = []
    for sym, items in by_symbol_raw.items():
        rets20 = [r["h_20"]["return_pct"] for r in items if r.get("h_20")]
        rets30 = [r["h_30"]["return_pct"] for r in items if r.get("h_30")]
        mfe20 = [r["h_20"]["mfe_pct"] for r in items if r.get("h_20")]
        mae20 = [abs(r["h_20"]["mae_pct"]) for r in items if r.get("h_20")]
        avg20 = np.mean(rets20) if rets20 else 0
        std20 = np.std(rets20, ddof=1) if len(rets20) > 1 else 0
        avg30 = np.mean(rets30) if rets30 else 0
        avg_mfe = np.mean(mfe20) if mfe20 else 0
        avg_mae = np.mean(mae20) if mae20 else 0
        ratio20 = avg_mfe / avg_mae if avg_mae > 0 else 0
        pe_pct = items[0].get("pe_percentile")
        concepts = items[0].get("concepts", [])
        market_states = [r.get("market_state", "unknown") for r in items]
        first_date = min(r["created_at"].strftime("%Y-%m-%d") for r in items)
        by_symbol_stats.append((sym, len(items), avg20, std20, avg30, avg_mfe, avg_mae, ratio20, pe_pct, concepts, market_states, first_date))
    by_symbol_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  按标的汇总: {len(by_symbol_stats)} 只标的")

    # ── 7. 终端摘要输出 ──
    print("\n" + "=" * 80)
    print("回测核心指标摘要 (仅 BUY)")
    print("=" * 80)
    if portfolio_rets:
        total_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] - 1) * 100
        ann_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] ** (252 / len(portfolio_rets)) - 1) * 100
        ann_vol = float(np.std(portfolio_rets, ddof=1) * np.sqrt(252))
        sr = sharpe_ratio(portfolio_rets)
        sor = sortino_ratio(portfolio_rets)
        mdd_info = max_drawdown_details(portfolio_rets)
        cal = calmar_ratio(portfolio_rets, mdd_info["mdd_pct"])
        print(f"  总收益: {total_ret:+.2f}%  年化: {ann_ret:+.2f}%  波动: {ann_vol:.2f}%")
        print(f"  Sharpe: {sr:.3f}  Sortino: {sor:.3f}  Calmar: {cal:.3f}")
        print(f"  MDD: {mdd_info['mdd_pct']:.2f}%  日胜率: {sum(1 for x in portfolio_rets if x>0)/len(portfolio_rets)*100:.1f}%")
    print(f"  BUY 有效: {len(valid_buys)}  标的: {len(by_symbol_stats)}")
    print(f"\n  Top 5 标的 (T+20):")
    for i, (sym, cnt, avg20, std20, avg30, mfe, mae, ratio, pe, concepts, mkt, first_date) in enumerate(by_symbol_stats[:5], 1):
        name = stock_names.get(sym, "?")
        concepts_str = ", ".join(concepts[:3]) if concepts else "-"
        print(f"    {i}. {sym} {name} ({first_date}, n={cnt}) T+20: {avg20:+.2f}%  T+30: {avg30:+.2f}%  MFE/MAE: {ratio:.2f}  PE分位: {pe or '-'}  [{concepts_str}]")
    print(f"\n  Bottom 5 标的 (T+20):")
    for i, (sym, cnt, avg20, std20, avg30, mfe, mae, ratio, pe, concepts, mkt, first_date) in enumerate(by_symbol_stats[-5:], 1):
        name = stock_names.get(sym, "?")
        concepts_str = ", ".join(concepts[:3]) if concepts else "-"
        print(f"    {i}. {sym} {name} ({first_date}, n={cnt}) T+20: {avg20:+.2f}%  T+30: {avg30:+.2f}%  MFE/MAE: {ratio:.2f}  PE分位: {pe or '-'}  [{concepts_str}]")

    # IC
    print(f"\n  IC:")
    for h in HORIZONS:
        info = ic_seq.get(h, {})
        ic = info.get("ic")
        if ic is not None:
            print(f"    T+{h}: {ic:+.4f} (N={info['n']}) {'✓' if abs(ic) > 0.05 else '✗'}")

    # PE分层
    print(f"\n  PE分位 × T+20:")
    pe_groups = defaultdict(list)
    for r in valid_buys:
        h20 = r.get("h_20", {}).get("return_pct") if r.get("h_20") else None
        if h20 is not None and r.get("pe_percentile") is not None:
            pct = r["pe_percentile"]
            if pct <= 20: pe_groups["0-20%"].append(h20)
            elif pct <= 40: pe_groups["20-40%"].append(h20)
            elif pct <= 60: pe_groups["40-60%"].append(h20)
            elif pct <= 80: pe_groups["60-80%"].append(h20)
            else: pe_groups["80-100%"].append(h20)
    for label in ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]:
        vals = pe_groups.get(label, [])
        if vals:
            print(f"    {label}: N={len(vals)} 均值={np.mean(vals):+.2f}%  胜率={sum(1 for x in vals if x>0)/len(vals)*100:.1f}%")

    # 涨停分析
    zdt_buys = [r for r in valid_buys if r.get("zdt_info")]
    non_zdt = [r for r in valid_buys if not r.get("zdt_info")]
    if zdt_buys:
        zdt20 = [r["h_20"]["return_pct"] for r in zdt_buys if r.get("h_20")]
        non20 = [r["h_20"]["return_pct"] for r in non_zdt if r.get("h_20")]
        print(f"\n  涨停日买入: {len(zdt_buys)}笔 T+20={np.mean(zdt20):+.2f}%")
        print(f"  非涨停日:   {len(non_zdt)}笔 T+20={np.mean(non20):+.2f}%")

    # ── 8. 生成图表 ──
    print("\n" + "=" * 80)
    print("生成可视化图表...")
    print("=" * 80)
    if portfolio_rets:
        total_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] - 1) * 100
        ann_ret = (np.cumprod(1 + np.array(portfolio_rets) / 100.0)[-1] ** (252 / len(portfolio_rets)) - 1) * 100
        ann_vol = float(np.std(portfolio_rets, ddof=1) * np.sqrt(252))
        sr = sharpe_ratio(portfolio_rets)
        mdd_info = max_drawdown_details(portfolio_rets)
        plot_equity_vs_bench(portfolio_rets, all_dates, bench_rets, bench_dts, total_ret, ann_ret, ann_vol, sr, mdd_info)
    buy_sorted = sorted(valid_buys, key=lambda r: r["created_at"])
    plot_per_stock_heatmap(buy_sorted, horizon=20)
    plot_mfe_mae_scatter_v2(valid_buys, horizon=20)
    plot_ic_decay(ic_seq)
    plot_valuation_vs_return(valid_buys)
    plot_risk_analysis_boxplot(valid_buys, horizon=20)
    plot_top_bottom_stocks(valid_buys, horizon=20, top_n=15)

    # ── 个股走势图 ──
    print("\n  生成个股走势图...")
    stock_chart_dir = CHARTS_DIR / "stock_charts"
    stock_chart_dir.mkdir(exist_ok=True)
    for sym in [bs[0] for bs in by_symbol_stats]:
        plot_stock_chart(sym, valid_buys, klines_cache, dates_cache, stock_names, stock_chart_dir)
    print(f"  个股走势图: {len(by_symbol_stats)} 张")

    # ── 9. 生成报告 ──
    print("\n" + "=" * 80)
    print("生成 Markdown 报告...")
    print("=" * 80)
    generate_report_md(
        buys_raw, valid_buys, wn_map, ns_map, fb_map, raw_map,
        stock_concepts, company_jsons, indicator_data, stock_names,
        portfolio_rets, all_dates, bench_rets, bench_dts,
        ic_seq, by_symbol_stats,
    )

    # ── 10. 导出个股明细 CSV ──
    print(f"\n导出个股明细 CSV: {CSV_PATH}")
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "symbol", "count", "T+20_mean", "T+20_std", "T+30_mean",
            "MFE20_mean", "MAE20_mean", "MFE_MAE_ratio",
            "PE_percentile", "PE_value", "PB_value", "total_mv",
            "main_concepts", "industry_dongcai", "market_states"
        ])
        for sym, cnt, avg20, std20, avg30, mfe20, mae20, ratio20, pe_pct, concepts, market_states, first_date in by_symbol_stats:
            comp = company_jsons.get(sym, {})
            writer.writerow([
                sym, cnt, round(avg20, 2), round(std20, 2), round(avg30, 2),
                round(mfe20, 2), round(mae20, 2), round(ratio20, 2),
                pe_pct if pe_pct else "",
                valid_buys[0].get("pe_value", "") if valid_buys else "",
                valid_buys[0].get("pb_value", "") if valid_buys else "",
                valid_buys[0].get("total_mv", "") if valid_buys else "",
                "|".join(concepts[:5]) if concepts else "",
                comp.get("industry_dongcai", ""),
                "|".join(market_states),
            ])

    print(f"\n{'=' * 80}")
    print(f"报告生成完成！")
    print(f"  报告文件: {REPORT_PATH}")
    print(f"  个股CSV:  {CSV_PATH}")
    print(f"  图表目录: {CHARTS_DIR.resolve()}")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
