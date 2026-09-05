#!/usr/bin/env python
"""检查已批准的买入操作后，股价是否跌破30分钟线的10周期均线(MA10)。

流程:
1. 从 quant_kb 数据库的 trading_operations 表读取 operation_type=buy 且
   status=approved 的记录，按时间排序。
2. 对每笔买入，加载该标的的1分钟线，聚合为30分钟线。
3. 计算30分钟线收盘价的10周期移动平均(MA10)。
4. 从买入时刻开始，逐根30分钟K线检查:
   - 收盘价是否跌破 MA10 (close < MA10)
   - 记录首次跌破的时间、距买入的K线数、跌幅等
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import psycopg2

# ── 配置 ──────────────────────────────────────────────────────────────
DB_NAME = "quant_kb"
DB_HOST = "localhost"
DB_PORT = 15432
DB_USER = "postgres"
DB_PASSWORD = "postgres"

KLINE_1M_DIR = Path("C:/klines/1m")
MA_PERIOD = 10          # 30分钟线的 MA 周期
KLINE_MIN_MIN_BARS = MA_PERIOD + 2  # 至少要这么根30分钟K线才能计算MA并检测跌破


# ── 数据库查询 ────────────────────────────────────────────────────────

def fetch_approved_buys(conn) -> list[dict]:
    """读取所有 buy + approved 的操作，按 created_at 升序。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, created_at, rationale, risk_level
        FROM trading_operations
        WHERE operation_type = 'buy' AND status = 'approved'
        ORDER BY created_at
        """
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0],
            "symbol": r[1],
            "created_at": r[2],
            "rationale": r[3],
            "risk_level": r[4],
        })
    cur.close()
    return rows


# ── 30分钟K线构建 ────────────────────────────────────────────────────

def _parse_1m_datetime(s: str) -> datetime:
    """解析1分钟K线的日期字段。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {s!r}")


def _is_trading_time(dt: datetime) -> bool:
    """判断1分钟bar是否处于交易时段。"""
    hm = dt.hour * 100 + dt.minute
    # 上午 9:30-11:30, 下午 13:00-15:00
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _bar_30m_end(dt: datetime) -> datetime:
    """给定一根1分钟bar的起始时间，返回其所属30分钟bar的结束时间。

    考虑交易时段：上午9:30-11:30，下午13:00-15:00。
    上午最后一根K线结束于11:30，下午最后一根结束于15:00。
    """
    hm = dt.hour * 100 + dt.minute

    if 930 <= hm <= 1130:
        # 上午盘：以09:30为基准
        anchor = dt.replace(hour=9, minute=30, second=0, microsecond=0)
        offset_min = int((dt - anchor).total_seconds() // 60)
        bucket = offset_min // 30
        end = anchor + timedelta(minutes=(bucket + 1) * 30)
        # 限制在11:30以内（11:30的bar应归入11:00-11:30桶）
        max_end = dt.replace(hour=11, minute=30, second=0, microsecond=0)
        return min(end, max_end)
    elif 1300 <= hm <= 1500:
        # 下午盘：以13:00为基准
        anchor = dt.replace(hour=13, minute=0, second=0, microsecond=0)
        offset_min = int((dt - anchor).total_seconds() // 60)
        bucket = offset_min // 30
        end = anchor + timedelta(minutes=(bucket + 1) * 30)
        # 限制在15:00以内（15:00的bar应归入14:30-15:00桶）
        max_end = dt.replace(hour=15, minute=0, second=0, microsecond=0)
        return min(end, max_end)
    else:
        # 非交易时段（不应出现，如果_is_trading_time正确的话）
        return dt


def load_30m_klines(symbol: str) -> Optional[pd.DataFrame]:
    """加载1分钟数据，聚合为30分钟线。

    返回 DataFrame，列: [trade_time(datetime), close, high, low, open]，
    按 trade_time 升序排列。如果数据不存在返回 None。
    """
    path = KLINE_1M_DIR / f"{symbol}.csv"
    if not path.exists():
        return None

    df = pd.read_csv(path, encoding="utf-8-sig")
    # 列名标准化
    df.rename(columns={
        "日期": "datetime_str",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "开盘": "open",
    }, inplace=True)

    if "datetime_str" not in df.columns:
        return None

    df["trade_time"] = df["datetime_str"].apply(_parse_1m_datetime)
    df = df[df["trade_time"].apply(_is_trading_time)].copy()
    df["bar_end"] = df["trade_time"].apply(_bar_30m_end)

    # 聚合: 每个30分钟桶内，open=第一根1分钟的open，close=最后一根的close，high=最高，low=最低
    agg = df.groupby("bar_end").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    agg.rename(columns={"bar_end": "trade_time"}, inplace=True)
    agg.sort_values("trade_time", inplace=True)
    agg.reset_index(drop=True, inplace=True)

    return agg


# ── 跌破检测 ─────────────────────────────────────────────────────────

def simulate_trade(
    df_30m: pd.DataFrame,
    buy_after: datetime,
    ma_period: int = MA_PERIOD,
) -> Optional[dict]:
    """模拟一笔交易：买入后持有，跌破30分钟MA10时卖出（T+1制度）。

    buy_after: 买入信号时间，从该时间之后的第一根30分钟K线开始交易。

    交易规则（A股T+1）：
    - 买入：在 buy_after 之后的第一根30分钟K线收盘价买入
    - 卖出：从买入日的下一个交易日开始，在第一根收盘价 < MA10 的K线收盘价卖出
    - 如果始终未跌破MA10，则持有到最后（以最后收盘价计算浮盈）

    返回:
        None: 数据不足
        dict: {
            "buy_time": 买入时间,
            "buy_price": 买入价格,
            "buy_date": 买入日期,
            "ma_at_buy": 买入时的MA值,
            "sell_time": 卖出时间（或最后数据时间）,
            "sell_price": 卖出价格,
            "bars_held": 持有K线数,
            "days_held": 持有交易日数,
            "return_pct": 收益率(%),
            "max_drawdown_pct": 持有期间最大回撤(%),
            "exit_reason": "break_ma" | "end_of_data",
        }
    """
    # 计算 MA
    if len(df_30m) < ma_period:
        return None
    df_30m = df_30m.copy()
    df_30m["ma"] = df_30m["close"].rolling(window=ma_period).mean()

    # 找到买入时间点：取 buy_after 之后（含）的第一根K线
    buy_mask = df_30m["trade_time"] >= buy_after
    if not buy_mask.any():
        return None
    buy_idx = df_30m[buy_mask].index[0]

    # MA 从第 ma_period-1 根开始有效
    if buy_idx < ma_period - 1:
        buy_idx = ma_period - 1

    buy_price = float(df_30m.loc[buy_idx, "close"])
    buy_time = df_30m.loc[buy_idx, "trade_time"]
    ma_at_buy = float(df_30m.loc[buy_idx, "ma"])
    buy_date = buy_time.date() if hasattr(buy_time, "date") else buy_time

    if pd.isna(ma_at_buy):
        return None

    # 找到买入日的下一个交易日（T+1限制：买入当天不能卖出）
    # 从买入K线之后，找到第一个日期不同的K线
    sell_start_idx = buy_idx + 1
    while sell_start_idx < len(df_30m):
        bar_time = df_30m.loc[sell_start_idx, "trade_time"]
        bar_date = bar_time.date() if hasattr(bar_time, "date") else bar_time
        if bar_date != buy_date:
            break  # 找到了下一个交易日
        sell_start_idx += 1
    else:
        # 买入日之后没有更多交易日了
        return {
            "buy_time": buy_time,
            "buy_price": buy_price,
            "buy_date": buy_date,
            "ma_at_buy": ma_at_buy,
            "sell_time": buy_time,
            "sell_price": buy_price,
            "bars_held": 0,
            "days_held": 0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "exit_reason": "end_of_data",
        }

    # 从下一个交易日开始，逐根检查是否跌破MA10
    max_price = buy_price
    max_dd = 0.0

    for idx in range(sell_start_idx, len(df_30m)):
        row = df_30m.loc[idx]
        if pd.isna(row["ma"]):
            continue

        close = float(row["close"])
        ma = float(row["ma"])
        trade_time = row["trade_time"]

        # 更新最大回撤
        if close > max_price:
            max_price = close
        dd = (close - buy_price) / buy_price * 100
        if dd < max_dd:
            max_dd = dd

        # 检测跌破MA10 → 卖出
        if close < ma:
            bars_held = idx - buy_idx
            sell_price = close
            sell_time = trade_time
            return_pct = (sell_price - buy_price) / buy_price * 100
            # 计算持有交易日数
            unique_dates = df_30m.loc[buy_idx:idx, "trade_time"].apply(
                lambda t: t.date() if hasattr(t, "date") else t
            ).nunique()
            return {
                "buy_time": buy_time,
                "buy_price": buy_price,
                "buy_date": buy_date,
                "ma_at_buy": ma_at_buy,
                "sell_time": sell_time,
                "sell_price": sell_price,
                "bars_held": bars_held,
                "days_held": unique_dates,
                "return_pct": round(return_pct, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "exit_reason": "break_ma",
            }

    # 始终未跌破MA10，持有到最后
    last_idx = len(df_30m) - 1
    last_row = df_30m.loc[last_idx]
    sell_price = float(last_row["close"])
    sell_time = last_row["trade_time"]
    bars_held = last_idx - buy_idx
    return_pct = (sell_price - buy_price) / buy_price * 100
    unique_dates = df_30m.loc[buy_idx:last_idx, "trade_time"].apply(
        lambda t: t.date() if hasattr(t, "date") else t
    ).nunique()

    return {
        "buy_time": buy_time,
        "buy_price": buy_price,
        "buy_date": buy_date,
        "ma_at_buy": ma_at_buy,
        "sell_time": sell_time,
        "sell_price": sell_price,
        "bars_held": bars_held,
        "days_held": unique_dates,
        "return_pct": round(return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "exit_reason": "end_of_data",
    }


# ── 分析 ─────────────────────────────────────────────────────────────

def analyze(ops: list[dict]):
    print(f"共加载 {len(ops)} 条 approved buy 操作\n")

    # 缓存30分钟K线
    kline_cache: dict[str, pd.DataFrame] = {}

    def _ensure_klines(sym: str):
        if sym not in kline_cache:
            df = load_30m_klines(sym)
            if df is not None and len(df) > 0:
                kline_cache[sym] = df
            else:
                kline_cache[sym] = None

    # 预加载
    symbols = sorted(set(o["symbol"] for o in ops if o["symbol"]))
    print(f"加载30分钟线 {len(symbols)} 个标的...")
    for i, sym in enumerate(symbols):
        _ensure_klines(sym)
        if (i + 1) % 20 == 0 or i + 1 == len(symbols):
            print(f"  {i + 1}/{len(symbols)}")

    valid_symbols = {s for s, df in kline_cache.items() if df is not None}
    ops_with_data = [o for o in ops if o["symbol"] in valid_symbols]
    ops_no_data = [o for o in ops if o["symbol"] not in valid_symbols]
    print(f"\n有效标的: {len(ops_with_data)} 条操作，"
          f"无数据跳过: {len(ops_no_data)} 条")

    # 逐条模拟交易
    trades: list[dict] = []
    for o in ops_with_data:
        df_30m = kline_cache[o["symbol"]]
        created_at = o["created_at"]

        # 去掉时区信息
        if hasattr(created_at, "tzinfo") and created_at.tzinfo is not None:
            created_at = created_at.replace(tzinfo=None)

        # 如果信号在15:00后，次日才能买入
        buy_after = created_at
        if hasattr(created_at, "hour") and created_at.hour >= 15:
            buy_after = created_at.replace(
                hour=9, minute=30, second=0, microsecond=0
            ) + timedelta(days=1)

        result = simulate_trade(df_30m, buy_after)
        if result is None:
            continue
        entry = {
            "id": o["id"],
            "symbol": o["symbol"],
            "created_at": created_at,
            "risk_level": o.get("risk_level"),
            **result,
        }
        trades.append(entry)

    # 分类统计
    broke_trades = [t for t in trades if t["exit_reason"] == "break_ma"]
    holding_trades = [t for t in trades if t["exit_reason"] == "end_of_data"]

    # ══════════════════════════════════════════════════════════════════
    # 一、策略总体表现
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("一、策略总体表现（买入后持有，跌破30分钟MA10卖出）")
    print(f"{'=' * 80}")
    print(f"  总交易笔数:   {len(trades):4d}")
    print(f"  触发卖出:     {len(broke_trades):4d} 笔 ({len(broke_trades) / max(len(trades), 1) * 100:.1f}%)")
    print(f"  仍持有:       {len(holding_trades):4d} 笔 ({len(holding_trades) / max(len(trades), 1) * 100:.1f}%)")

    if not trades:
        print("\n  无有效交易。")
        return

    returns = [t["return_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    drawdowns = [t["max_drawdown_pct"] for t in trades]

    print(f"\n  ── 收益率统计 ──")
    print(f"  平均收益率:   {sum(returns) / len(returns):+.2f}%")
    print(f"  中位数收益:   {sorted(returns)[len(returns) // 2]:+.2f}%")
    print(f"  胜率:         {len(wins) / len(returns) * 100:.1f}% ({len(wins)}/{len(returns)})")
    print(f"  最佳交易:     {max(returns):+.2f}%")
    print(f"  最差交易:     {min(returns):+.2f}%")

    if wins:
        print(f"  盈利交易平均: +{sum(wins) / len(wins):.2f}%")
    if losses:
        print(f"  亏损交易平均: {sum(losses) / len(losses):.2f}%")

    print(f"\n  ── 持有期间最大回撤 ──")
    print(f"  平均最大回撤: {sum(drawdowns) / len(drawdowns):+.2f}%")
    print(f"  最小回撤:     {max(drawdowns):+.2f}%")
    print(f"  最大回撤:     {min(drawdowns):+.2f}%")

    # 持有时间
    bars_held = [t["bars_held"] for t in trades]
    days_held = [t["days_held"] for t in trades]
    print(f"\n  ── 持有时间 ──")
    print(f"  平均持有:     {sum(bars_held) / len(bars_held):.1f} 根K线 ({sum(days_held) / len(days_held):.1f} 个交易日)")
    print(f"  最短持有:     {min(bars_held)} 根K线 ({min(days_held)} 天)")
    print(f"  最长持有:     {max(bars_held)} 根K线 ({max(days_held)} 天)")

    # ══════════════════════════════════════════════════════════════════
    # 二、收益分布
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("二、收益分布")
    print(f"{'=' * 80}")
    ret_buckets = {
        "< -10%": 0, "-10%~-5%": 0, "-5%~-3%": 0, "-3%~-1%": 0,
        "-1%~0%": 0, "0~1%": 0, "1~3%": 0, "3~5%": 0,
        "5~10%": 0, "> 10%": 0,
    }
    for r in returns:
        if r < -10:
            ret_buckets["< -10%"] += 1
        elif r < -5:
            ret_buckets["-10%~-5%"] += 1
        elif r < -3:
            ret_buckets["-5%~-3%"] += 1
        elif r < -1:
            ret_buckets["-3%~-1%"] += 1
        elif r < 0:
            ret_buckets["-1%~0%"] += 1
        elif r < 1:
            ret_buckets["0~1%"] += 1
        elif r < 3:
            ret_buckets["1~3%"] += 1
        elif r < 5:
            ret_buckets["3~5%"] += 1
        elif r < 10:
            ret_buckets["5~10%"] += 1
        else:
            ret_buckets["> 10%"] += 1

    for label, cnt in ret_buckets.items():
        bar = "█" * (cnt * 40 // len(trades)) if trades else ""
        print(f"  {label:>10s}: {cnt:3d} ({cnt / len(trades) * 100:5.1f}%) {bar}")

    # ══════════════════════════════════════════════════════════════════
    # 三、按标的汇总
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("三、按标的汇总")
    print(f"{'=' * 80}")
    by_sym: dict[str, list] = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t["return_pct"])

    sym_stats = []
    for sym, rets in by_sym.items():
        avg_ret = sum(rets) / len(rets)
        win_cnt = sum(1 for r in rets if r > 0)
        sym_stats.append((sym, len(rets), avg_ret, win_cnt / len(rets) * 100))
    sym_stats.sort(key=lambda x: x[2], reverse=True)

    print(f"  {'标的':<12s} {'次数':>4s} {'平均收益':>9s} {'胜率':>6s}")
    print(f"  {'-' * 35}")
    for sym, cnt, avg_r, wr in sym_stats:
        print(f"  {sym:<12s} {cnt:4d} {avg_r:+8.2f}% {wr:5.0f}%")

    # ══════════════════════════════════════════════════════════════════
    # 四、按风险等级汇总
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("四、按风险等级汇总")
    print(f"{'=' * 80}")
    by_risk: dict[str, list] = defaultdict(list)
    for t in trades:
        risk = t.get("risk_level") or "unknown"
        by_risk[risk].append(t["return_pct"])

    print(f"  {'风险':<12s} {'次数':>4s} {'平均收益':>9s} {'胜率':>6s}")
    print(f"  {'-' * 35}")
    for risk in sorted(by_risk.keys()):
        rets = by_risk[risk]
        avg_r = sum(rets) / len(rets)
        win_cnt = sum(1 for r in rets if r > 0)
        print(f"  {risk:<12s} {len(rets):4d} {avg_r:+8.2f}% {win_cnt / len(rets) * 100:5.0f}%")

    # ══════════════════════════════════════════════════════════════════
    # 五、交易明细（按时间排序）
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("五、交易明细（按时间排序）")
    print(f"{'=' * 80}")
    trades_sorted = sorted(trades, key=lambda t: t["created_at"])
    print(f"  {'买入时间':<18s} {'标的':<12s} {'买入价':>8s} {'卖出时间':<18s} {'卖出价':>8s} "
          f"{'天数':>4s} {'收益':>7s} {'最大回撤':>8s} {'退出原因':>10s}")
    print(f"  {'-' * 100}")
    for t in trades_sorted:
        buy_t = t["buy_time"].strftime("%Y-%m-%d %H:%M") if hasattr(t["buy_time"], "strftime") else str(t["buy_time"])
        sell_t = t["sell_time"].strftime("%Y-%m-%d %H:%M") if hasattr(t["sell_time"], "strftime") else str(t["sell_time"])
        exit_reason = "破MA卖出" if t["exit_reason"] == "break_ma" else "数据结束"
        print(f"  {buy_t:<18s} {t['symbol']:<12s} {t['buy_price']:8.2f} {sell_t:<18s} {t['sell_price']:8.2f} "
              f"{t['days_held']:4d} {t['return_pct']:+6.2f}% {t['max_drawdown_pct']:+7.2f}% {exit_reason:>10s}")


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
        ops = fetch_approved_buys(conn)
        analyze(ops)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
