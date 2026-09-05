#!/usr/bin/env python
"""分析 trading_operations 的收益率表现。

从 quant_kb 数据库读取所有 trading_operations，结合本地日线数据计算买入后
5/10/20/30 个交易日的收益率和最大回撤，并按 operation_type、symbol、时间等
维度输出详细统计。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Optional

import psycopg2

# ── 配置 ──────────────────────────────────────────────────────────────
DB_NAME = "quant_kb"
DB_HOST = "localhost"
DB_PORT = 15432
DB_USER = "postgres"
DB_PASSWORD = "postgres"

KLINES_DIR = Path("C:/klines/daily")
INDEX_DIR = Path("C:/klines/index_daily")
HORIZONS = [5, 10, 20, 30]  # 交易日
BENCHMARKS = {"创业板指(399006)": "创业板指", "中证500(399905)": "中证500"}

# ── 数据库查询 ────────────────────────────────────────────────────────


def fetch_operations(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, operation_type, symbol, created_at, status,
               rationale, risk_level
        FROM trading_operations
        ORDER BY created_at
        """
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0],
            "operation_type": r[1],
            "symbol": r[2],
            "created_at": r[3],
            "status": r[4],
            "rationale": r[5],
            "risk_level": r[6],
        })
    cur.close()
    return rows


# ── 日线数据 ──────────────────────────────────────────────────────────


def _normalize_date(d: str) -> str:
    """统一将 YYYYMMDD 转为 YYYY-MM-DD，已经是YYYY-MM-DD的不变。"""
    d = d.strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _to_date(dt):
    """从 date 或 datetime 提取 date 对象。"""
    if hasattr(dt, "date"):
        return dt.date()
    return dt


def _effective_buy_date(created_at) -> str:
    """确定有效的买入参考日期（YYYY-MM-DD）。

    如果信号在收盘后（>=15:00）产生，实际只能在下一个交易日执行，
    不能以当天收盘价成交，推后一个自然日。
    """
    d = _to_date(created_at)
    if hasattr(created_at, "hour") and created_at.hour >= 15:
        d = d + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def load_klines(symbol: str) -> dict[str, dict]:
    """返回 {trade_date_str(YYYY-MM-DD): {open, high, low, close, pct_chg}} 的映射。"""
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
    """获取某日或之后最近一个有效交易日的收盘价、日期和索引。

    Returns: (close, found_date, idx) 三者同时非 None 或同时为 None。
    """
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
    """计算买入后 n 个交易日的收益率和期间最大回撤。

    start_idx 由 get_close_on_or_after 返回，保证指向买入参考日。
    Returns: {horizon: {"return_pct": float, "max_drawdown_pct": float} or None}
    None 表示数据不足。
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
        max_dd = 0.0
        for j in range(start_idx + 1, end_idx + 1):
            price = klines[dates[j]]["close"]
            if price > max_price:
                max_price = price
            dd = (max_price - price) / max_price * 100
            if dd > max_dd:
                max_dd = dd

        result[h] = {"return_pct": round(ret_pct, 2), "max_drawdown_pct": round(max_dd, 2), "end_date": dates[end_idx]}

    return result


def _compute_returns_for_ops(
    ops: list[dict],
    klines_cache: dict[str, dict[str, dict]],
    dates_cache: dict[str, list[str]],
) -> dict[int, list[dict]]:
    """为一批操作统一计算各 horizon 的收益率。

    dates_cache 预排序，避免每个操作重复排序。
    返回 {horizon: [entry, ...]}。
    """
    results: dict[int, list[dict]] = defaultdict(list)
    for o in ops:
        klines = klines_cache.get(o["symbol"], {})
        if not klines:
            continue
        effective_date = _effective_buy_date(o["created_at"])
        dates = dates_cache[o["symbol"]]
        close, found_date, idx = get_close_on_or_after(dates, klines, effective_date)
        if close is None or idx is None:
            continue
        rets = get_next_n_returns(dates, klines, idx, close)
        s = o.get("status", "")
        for h, r in rets.items():
            if r is not None:
                entry = {
                    **r,
                    "symbol": o["symbol"],
                    "created_at": o["created_at"],
                    "buy_price": close,
                    "buy_date": found_date,
                    "status": s,
                }
                results[h].append(entry)
    return results


# ── 分析 ──────────────────────────────────────────────────────────────


def analyze(ops: list[dict]):
    # ── 预处理：按买入参考日过滤数据不足的记录 ──
    klines_cache: dict[str, dict[str, dict]] = {}
    dates_cache: dict[str, list[str]] = {}

    def _ensure_klines(sym: str):
        if sym not in klines_cache:
            klines_cache[sym] = load_klines(sym)
            dates_cache[sym] = _sorted_dates(klines_cache[sym])

    buys = [o for o in ops if o["operation_type"] == "buy" and o["symbol"]]
    sells = [o for o in ops if o["operation_type"] == "sell" and o["symbol"]]
    skips = [o for o in ops if o["operation_type"] == "skip" and o["symbol"]]

    # 预加载所有需要的 klines
    symbols_needed = set(o["symbol"] for o in buys + skips)
    print(f"加载日线数据 {len(symbols_needed)} 个标的...")
    for i, sym in enumerate(sorted(symbols_needed)):
        _ensure_klines(sym)
        if (i + 1) % 20 == 0 or i + 1 == len(symbols_needed):
            print(f"  {i + 1}/{len(symbols_needed)}")

    # 过滤：如果最近交易日到买入参考日后不足 MIN_DATA_TRADING_DAYS 条数据，则跳过
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
    if len(skips_filtered) < len(skips):
        print(f"  过滤掉 {len(skips) - len(skips_filtered)} 条 skip（数据不足）")

    buys_approved = [o for o in buys_filtered if o["status"] == "approved"]
    buys_rejected = [o for o in buys_filtered if o["status"] == "rejected"]
    buys_pending = [o for o in buys_filtered if o["status"] == "pending"]
    buys_triggered_close = [o for o in buys_filtered if o["status"] == "triggered_close"]

    print()
    print(f"总记录: {len(ops)} 条")
    print(f"  buy : {len(buys_filtered)} 条（原始 {len(buys)}，过滤 {len(buys) - len(buys_filtered)}）")
    print(f"    approved:       {len(buys_approved)}")
    print(f"    rejected:       {len(buys_rejected)}")
    print(f"    pending:        {len(buys_pending)}")
    print(f"    triggered_close:{len(buys_triggered_close)}")
    print(f"  sell: {len(sells)} 条")
    print(f"  skip: {len(skips_filtered)} 条（原始 {len(skips)}）")
    print()

    # 加载基准指数数据
    benchmark_data: dict[str, dict[str, dict]] = {}
    benchmark_dates: dict[str, list[str]] = {}
    print(f"加载基准指数: {list(BENCHMARKS.keys())}")
    for name, code in BENCHMARKS.items():
        benchmark_data[name] = load_klines(code)
        benchmark_dates[name] = _sorted_dates(benchmark_data[name])
        print(f"  {name} ({code}): {len(benchmark_data[name])} 个交易日")
    print()

    # ═══════════════════════════════════════════
    # 1. 买入收益分析（按 horizon）
    # ═══════════════════════════════════════════
    print("=" * 80)
    print("一、买入后收益率统计（按持有天数）")
    print("=" * 80)

    horizon_results = _compute_returns_for_ops(buys_filtered, klines_cache, dates_cache)

    # 按 status 分组
    status_results: dict[str, dict[int, list[dict]]] = {
        "approved": defaultdict(list),
        "rejected": defaultdict(list),
        "pending": defaultdict(list),
        "triggered_close": defaultdict(list),
    }
    for h, entries in horizon_results.items():
        for e in entries:
            s = e["status"]
            if s in status_results:
                status_results[s][h].append(e)

    for h in HORIZONS:
        results = horizon_results.get(h, [])
        if not results:
            print(f"\n--- {h} 日收益率（数据不足） ---")
            continue

        rets = [r["return_pct"] for r in results]
        dds = [r["max_drawdown_pct"] for r in results]
        win = [r for r in rets if r > 0]

        avg_ret = sum(rets) / len(rets)
        median_ret = sorted(rets)[len(rets) // 2]
        avg_dd = sum(dds) / len(dds)
        max_dd_val = max(dds)
        win_rate = len(win) / len(rets) * 100 if rets else 0
        best = max(rets)
        worst = min(rets)

        print(f"\n--- {h} 个交易日（N={len(results)}）---")
        print(f"  平均收益率:    {avg_ret:+.2f}%")
        print(f"  中位数收益率:  {median_ret:+.2f}%")
        print(f"  胜率:          {win_rate:.1f}% ({len(win)}/{len(rets)})")
        print(f"  最佳:          {best:+.2f}%")
        print(f"  最差:          {worst:+.2f}%")
        print(f"  平均最大回撤:  {avg_dd:.2f}%")
        print(f"  最大回撤:      {max_dd_val:.2f}%")

        # 收益分布
        buckets = {"< -10%": 0, "-10%~-5%": 0, "-5%~-2%": 0, "-2%~0%": 0, "0~2%": 0, "2%~5%": 0, "5%~10%": 0, "> 10%": 0}
        for ret in rets:
            if ret < -10:
                buckets["< -10%"] += 1
            elif ret < -5:
                buckets["-10%~-5%"] += 1
            elif ret < -2:
                buckets["-5%~-2%"] += 1
            elif ret < 0:
                buckets["-2%~0%"] += 1
            elif ret < 2:
                buckets["0~2%"] += 1
            elif ret < 5:
                buckets["2%~5%"] += 1
            elif ret < 10:
                buckets["5%~10%"] += 1
            else:
                buckets["> 10%"] += 1

        print(f"  收益分布:")
        for label, cnt in buckets.items():
            bar = "█" * (cnt * 40 // len(rets)) if rets else ""
            print(f"    {label:>10s}: {cnt:3d} ({cnt / len(rets) * 100:5.1f}%) {bar}")

    # ═══════════════════════════════════════════
    # 1.5. 基准指数收益率
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("一.五、基准指数同期表现对比")
    print("=" * 80)

    print(f"\n  {'指标':<20s} {'N':>4s}  {'平均收益':>8s}  {'中位数':>8s}  {'胜率':>6s}  {'最大回撤':>8s}  {'平均回撤':>8s}")
    print(f"  {'-' * 90}")

    for name in BENCHMARKS:
        idx_klines = benchmark_data[name]
        idx_dates = benchmark_dates[name]
        idx_rets_by_h: dict[int, list[float]] = defaultdict(list)
        idx_dds_by_h: dict[int, list[float]] = defaultdict(list)

        for o in buys_filtered:
            effective_date = _effective_buy_date(o["created_at"])
            close, _, idx = get_close_on_or_after(idx_dates, idx_klines, effective_date)
            if close is None or idx is None:
                continue
            rets = get_next_n_returns(idx_dates, idx_klines, idx, close)
            for h, r in rets.items():
                if r is not None:
                    idx_rets_by_h[h].append(r["return_pct"])
                    idx_dds_by_h[h].append(r["max_drawdown_pct"])

        for h in HORIZONS:
            idx_rets = idx_rets_by_h.get(h, [])
            idx_dds = idx_dds_by_h.get(h, [])
            if not idx_rets:
                continue
            avg_r = sum(idx_rets) / len(idx_rets)
            med_r = sorted(idx_rets)[len(idx_rets) // 2]
            wr = sum(1 for x in idx_rets if x > 0) / len(idx_rets) * 100
            avg_dd = sum(idx_dds) / len(idx_dds)
            max_dd_idx = max(idx_dds)
            b_rets = [r["return_pct"] for r in horizon_results.get(h, [])]
            b_avg = sum(b_rets) / len(b_rets) if b_rets else 0
            excess = b_avg - avg_r if b_rets else 0
            print(f"  {name + f'({h}日)':<20s} {len(idx_rets):4d}  {avg_r:+7.2f}%  {med_r:+7.2f}%  {wr:5.1f}%  {max_dd_idx:+6.2f}%  {avg_dd:+7.2f}%  (Buy超额: {excess:+.2f}%)")

    # ═══════════════════════════════════════════
    # 1.6. 各status分组 vs 基准 对比
    # ═══════════════════════════════════════════
    print("\n--- 各状态 vs 基准指数（30日） ---")
    print(f"  {'分组':<20s} {'N':>4s}  {'平均收益':>8s}  {'超额(创业板)':>10s}  {'超额(中证500)':>10s}")
    print(f"  {'-' * 75}")

    idx_30_ret = {}
    for name in BENCHMARKS:
        idx_k = benchmark_data[name]
        idx_d = benchmark_dates[name]
        rets = []
        for o in buys_filtered:
            effective_date = _effective_buy_date(o["created_at"])
            close, _, idx = get_close_on_or_after(idx_d, idx_k, effective_date)
            if close is not None and idx is not None:
                r = get_next_n_returns(idx_d, idx_k, idx, close)
                if 30 in r and r[30] is not None:
                    rets.append(r[30]["return_pct"])
        idx_30_ret[name] = sum(rets) / len(rets) if rets else 0

    for label in ["approved", "rejected", "pending", "triggered_close"]:
        items = status_results[label].get(30, [])
        if not items:
            continue
        avg_r = sum(it["return_pct"] for it in items) / len(items)
        excess_cy = avg_r - idx_30_ret["创业板指(399006)"]
        excess_zz = avg_r - idx_30_ret["中证500(399905)"]
        print(f"  {label:<20s} {len(items):4d}  {avg_r:+7.2f}%  {excess_cy:+10.2f}%  {excess_zz:+10.2f}%")

    # ═══════════════════════════════════════════
    # 2. buy vs skip 对比
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("二、Buy vs Skip 收益对比")
    print("=" * 80)

    skip_results = _compute_returns_for_ops(skips_filtered, klines_cache, dates_cache)

    for h in HORIZONS:
        b = [r["return_pct"] for r in horizon_results.get(h, [])]
        s = [r["return_pct"] for r in skip_results.get(h, [])]

        if not b and not s:
            continue

        print(f"\n--- {h} 个交易日 ---")

        avg_b = None
        avg_s = None
        if b:
            avg_b = sum(b) / len(b)
            median_b = sorted(b)[len(b) // 2]
            win_b = sum(1 for x in b if x > 0) / len(b) * 100
            print(f"  Buy  (N={len(b):3d}): 平均 {avg_b:+.2f}%  中位数 {median_b:+.2f}%  胜率 {win_b:.1f}%")
        else:
            print(f"  Buy  (N=0): 无数据")

        if s:
            avg_s = sum(s) / len(s)
            median_s = sorted(s)[len(s) // 2]
            win_s = sum(1 for x in s if x > 0) / len(s) * 100
            print(f"  Skip (N={len(s):3d}): 平均 {avg_s:+.2f}%  中位数 {median_s:+.2f}%  胜率 {win_s:.1f}%")
        else:
            print(f"  Skip (N=0): 无数据")

        if avg_b is not None and avg_s is not None:
            diff = avg_b - avg_s
            print(f"  Buy - Skip 差值: {diff:+.2f}% {'← buy更优' if diff > 0 else '← skip更优（避开亏损）'}")

    # ═══════════════════════════════════════════
    # 3. 最大回撤详情
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("三、最大回撤分布（Buy 操作）")
    print("=" * 80)

    for h in HORIZONS:
        results = horizon_results.get(h, [])
        if not results:
            continue

        dds = [r["max_drawdown_pct"] for r in results]
        print(f"\n--- {h} 日最大回撤（N={len(dds)}）---")
        print(f"  均值:   {sum(dds) / len(dds):.2f}%")
        print(f"  中位数: {sorted(dds)[len(dds) // 2]:.2f}%")
        print(f"  最大值: {max(dds):.2f}%")
        print(f"  最小值: {min(dds):.2f}%")

        ranked = sorted(results, key=lambda r: r["return_pct"])
        print(f"  收益最差的5笔:")
        for i, r in enumerate(ranked[:5], 1):
            print(f"    {i}. {r['symbol']} {r['created_at'].strftime('%Y-%m-%d')} "
                  f"收益 {r['return_pct']:+.2f}%  最大回撤 {r['max_drawdown_pct']:.2f}%")
        print(f"  收益最好的5笔:")
        for i, r in enumerate(ranked[-5:][::-1], 1):
            print(f"    {i}. {r['symbol']} {r['created_at'].strftime('%Y-%m-%d')} "
                  f"收益 {r['return_pct']:+.2f}%  最大回撤 {r['max_drawdown_pct']:.2f}%")

    # ═══════════════════════════════════════════
    # 4. 按风险等级分组
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("四、按风险等级分组收益（Buy，30日）")
    print("=" * 80)

    results_30 = horizon_results.get(30, [])
    if results_30:
        by_risk: dict[str, list] = defaultdict(list)
        for o in buys_filtered:
            risk = o.get("risk_level") or "unknown"
            for r in results_30:
                if r["created_at"] == o["created_at"] and r["symbol"] == o["symbol"]:
                    by_risk[risk].append(r["return_pct"])
                    break

        for risk, rets in sorted(by_risk.items()):
            avg_r = sum(rets) / len(rets) if rets else 0
            print(f"  {risk:>10s} (N={len(rets):3d}): 平均 30日收益 {avg_r:+.2f}%")

    # ═══════════════════════════════════════════
    # 5. 按时间分布
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("五、按周统计 Buy 表现（20日收益）")
    print("=" * 80)

    results_20 = horizon_results.get(20, [])
    if results_20:
        by_week: dict[str, list] = defaultdict(list)
        for r in results_20:
            wk = r["created_at"].strftime("%Y-W%W")
            by_week[wk].append(r["return_pct"])

        for wk, rets in sorted(by_week.items()):
            avg_r = sum(rets) / len(rets) if rets else 0
            win = sum(1 for x in rets if x > 0)
            print(f"  {wk}: N={len(rets):3d}  平均 {avg_r:+.2f}%  胜率 {win / len(rets) * 100:.0f}%")

    # ═══════════════════════════════════════════
    # 6. Skip 决策质量分析
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("六、Skip 决策质量分析（如果 Skip 后被 Skip 的标的涨了，说明错过机会）")
    print("=" * 80)

    for h in HORIZONS:
        s_data = skip_results.get(h, [])
        if not s_data:
            continue
        s_rets = [r["return_pct"] for r in s_data]
        missed_up = sum(1 for x in s_rets if x > 5)
        missed_huge = sum(1 for x in s_rets if x > 10)
        avoided_loss = sum(1 for x in s_rets if x < -5)
        avoided_big = sum(1 for x in s_rets if x < -10)

        print(f"\n--- {h} 日（N={len(s_rets)}）---")
        print(f"  正确跳过（下跌 > 5%）:  {avoided_loss:3d}  ({avoided_loss / len(s_rets) * 100:5.1f}%) 规避了较大亏损")
        print(f"  正确跳过（下跌 > 10%）: {avoided_big:3d}  ({avoided_big / len(s_rets) * 100:5.1f}%) 规避了重大亏损")
        print(f"  错过涨幅（上涨 > 5%）:  {missed_up:3d}  ({missed_up / len(s_rets) * 100:5.1f}%) 错过了较大涨幅")
        print(f"  错过涨幅（上涨 > 10%）: {missed_huge:3d}  ({missed_huge / len(s_rets) * 100:5.1f}%) 错过了重大涨幅")

    # ═══════════════════════════════════════════
    # 7. 按标的汇总
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("七、按标的汇总（30日收益）")
    print("=" * 80)

    if results_30:
        by_symbol: dict[str, list[float]] = defaultdict(list)
        for r in results_30:
            by_symbol[r["symbol"]].append(r["return_pct"])

        sym_stats = []
        for sym, rets in by_symbol.items():
            avg_r = sum(rets) / len(rets)
            win = sum(1 for x in rets if x > 0)
            sym_stats.append((sym, len(rets), avg_r, win / len(rets) * 100))
        sym_stats.sort(key=lambda x: x[2], reverse=True)

        print(f"  {'标的':<12s} {'次数':>4s}  {'平均收益':>8s}  {'胜率':>6s}")
        print(f"  {'-' * 35}")
        for sym, cnt, avg_r, wr in sym_stats:
            print(f"  {sym:<12s} {cnt:4d}  {avg_r:+7.2f}%  {wr:5.0f}%")

    # ═══════════════════════════════════════════
    # 8. Buy approved vs rejected 对比
    # ═══════════════════════════════════════════
    print("\n" + "=" * 80)
    print("八、Buy 批准(approved) vs 拒绝(rejected) 收益对比")
    print("=" * 80)

    approved_res = status_results["approved"]
    rejected_res = status_results["rejected"]
    pending_res = status_results["pending"]
    triggered_close_res = status_results["triggered_close"]

    print(f"\n样本量: approved={sum(len(v) for v in approved_res.values())}, "
          f"rejected={sum(len(v) for v in rejected_res.values())}, "
          f"pending={sum(len(v) for v in pending_res.values())}, "
          f"triggered_close={sum(len(v) for v in triggered_close_res.values())}")

    for h in HORIZONS:
        print(f"\n--- {h} 个交易日 ---")
        print(f"  {'状态':<20s} {'N':>4s}  {'平均收益':>8s}  {'中位数':>8s}  {'胜率':>6s}  {'最大回撤':>8s}  {'平均回撤':>8s}")
        print(f"  {'-' * 80}")

        for label, res_map in [
            ("approved", approved_res),
            ("rejected", rejected_res),
            ("pending", pending_res),
            ("triggered_close", triggered_close_res),
        ]:
            items = res_map.get(h, [])
            if not items:
                print(f"  {label:<20s} {'0':>4s}  {'-':>8s}  {'-':>8s}  {'-':>6s}  {'-':>8s}  {'-':>8s}")
                continue
            rets = [it["return_pct"] for it in items]
            dds = [it["max_drawdown_pct"] for it in items]
            avg_r = sum(rets) / len(rets)
            med_r = sorted(rets)[len(rets) // 2]
            win_r = sum(1 for x in rets if x > 0) / len(rets) * 100
            avg_dd = sum(dds) / len(dds)
            max_dd_val = max(dds)
            print(f"  {label:<20s} {len(items):4d}  {avg_r:+7.2f}%  {med_r:+7.2f}%  {win_r:5.1f}%  {max_dd_val:+6.2f}%  {avg_dd:+7.2f}%")

        # approved vs rejected 差值
        a_items = approved_res.get(h, [])
        r_items = rejected_res.get(h, [])
        if a_items and r_items:
            a_rets = [it["return_pct"] for it in a_items]
            r_rets = [it["return_pct"] for it in r_items]
            diff = sum(a_rets) / len(a_rets) - sum(r_rets) / len(r_rets)
            print(f"  approved - rejected 差值: {diff:+.2f}% {'← 审批通过更优' if diff > 0 else '← 拒绝反而更优'}")

    # 展示 approved/rejected 各标的明细
    print("\n--- approved 明细（30日） ---")
    a_items = approved_res.get(30, [])
    if a_items:
        a_items.sort(key=lambda x: x["return_pct"], reverse=True)
        for it in a_items:
            print(f"  {it['symbol']:<12s} {it['created_at'].strftime('%Y-%m-%d')}  "
                  f"收益 {it['return_pct']:+7.2f}%  最大回撤 {it['max_drawdown_pct']:.2f}%")

    print("\n--- rejected 明细（30日） ---")
    r_items = rejected_res.get(30, [])
    if r_items:
        r_items.sort(key=lambda x: x["return_pct"], reverse=True)
        for it in r_items:
            print(f"  {it['symbol']:<12s} {it['created_at'].strftime('%Y-%m-%d')}  "
                  f"收益 {it['return_pct']:+7.2f}%  最大回撤 {it['max_drawdown_pct']:.2f}%")

    # 按标的汇总 approved vs rejected
    print("\n--- 按标的汇总 approved vs rejected（30日） ---")
    a_by_sym: dict[str, list[float]] = defaultdict(list)
    r_by_sym: dict[str, list[float]] = defaultdict(list)
    for it in a_items:
        a_by_sym[it["symbol"]].append(it["return_pct"])
    for it in r_items:
        r_by_sym[it["symbol"]].append(it["return_pct"])

    all_syms = sorted(set(list(a_by_sym.keys()) + list(r_by_sym.keys())))
    if all_syms:
        print(f"  {'标的':<12s} {'approved':>20s}  {'rejected':>20s}  {'diff':>8s}")
        print(f"  {'-' * 65}")
        for sym in all_syms:
            a_vals = a_by_sym.get(sym, [])
            r_vals = r_by_sym.get(sym, [])
            a_str = f"N={len(a_vals):2d} {sum(a_vals)/len(a_vals):+.2f}%" if a_vals else "-"
            r_str = f"N={len(r_vals):2d} {sum(r_vals)/len(r_vals):+.2f}%" if r_vals else "-"
            if a_vals and r_vals:
                diff_s = f"{(sum(a_vals)/len(a_vals) - sum(r_vals)/len(r_vals)):+.2f}%"
            else:
                diff_s = "-"
            print(f"  {sym:<12s} {a_str:>20s}  {r_str:>20s}  {diff_s:>8s}")


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
        analyze(ops)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
