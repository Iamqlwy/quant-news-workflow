"""原子实现正确性验证 —— 对每个原子独立重算，与 atom 结果比对"""
from __future__ import annotations
from loguru import logger

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
settings.simulation_enabled = True

import numpy as np
import pandas as pd

from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider
from src.market import loader as _loader
from src.triggers.evaluators import evaluate_atom
from src.triggers.evaluators.intraday import eval_intraday_shot_up_fall

CLOCK = Clock(TimeConfig(
    start_time=datetime(2026, 5, 25, 11, 0, 0),
    tick_duration=timedelta(minutes=5),
    realtime=False,
))

TICKER = "000001.SZ"
SECTOR = "智能电网"
SECTOR_CODE = "885311.TI"

# ── 辅助函数：获取同一数据源的原始数据，避免 atom 内部缓存影响 ──

def get_tech(ticker: str, market: MarketDataProvider) -> dict:
    """获取技术指标（绕过缓存，直接调用计算函数）"""
    from src.market.data import _calc_indicators
    df = market._get_daily_df(ticker)
    if df is None or len(df) < 10:
        return {}
    df = market._truncate_for_clock(df, is_intraday=False, ticker=ticker)
    if df is None or len(df) < 10:
        return {}
    return _calc_indicators(df.tail(60))

def get_price_data(ticker: str, market: MarketDataProvider) -> dict:
    """获取实时价格数据"""
    return market._realtime_price_from_csv(ticker)

def get_intraday(ticker: str, market: MarketDataProvider) -> dict:
    """获取日内快照"""
    return market._csv_intraday_snapshot(ticker)


# ═══════════════════════════════════════════════
# 逐原子验证
# ═══════════════════════════════════════════════

async def verify_all(market: MarketDataProvider):
    results: list[dict] = []

    tech = get_tech(TICKER, market)
    price_data = get_price_data(TICKER, market)
    intraday = get_intraday(TICKER, market)
    daily_df = market._get_daily_df(TICKER)
    daily_trunc = market._truncate_for_clock(daily_df, is_intraday=False, ticker=TICKER)

    # ── price_level ──
    async def check(name, params, expected_triggered, notes=""):
        atom = await evaluate_atom(name, params, market)
        triggered = atom["triggered"]
        passed = triggered == expected_triggered
        status = "✓" if passed else "✗ MISMATCH"
        if not passed:
            logger.info(f"  {status} {name}: atom={triggered} expected={expected_triggered} | {notes}")
            logger.info(f"       atom_detail={atom.get('detail', atom.get('error', ''))}")
        results.append({"name": name, "passed": passed, "atom": triggered, "expected": expected_triggered})
        return passed

    logger.info("=" * 70)
    logger.info(f"  原子正确性验证  |  {TICKER}  |  {CLOCK.now}")
    logger.info("=" * 70)

    # ═══ 价格与成交量 ═══
    logger.info("\n── 价格与成交量 ──")

    # price_level: price vs level
    price = tech.get("latest_close", 0)
    ma20 = tech.get("ma", {}).get("MA20", 0)
    expected = price > 10.0  # level=10.0, relation=above
    await check("price_level", {"ticker": TICKER, "level": 10.0, "relation": "above"}, expected,
                f"price={price} > 10.0")

    # price_change_pct: 涨跌幅 vs threshold
    pct_chg = price_data.get("pct_chg", 0)
    expected = pct_chg >= 1.0  # direction=up, pct=1.0
    await check("price_change_pct", {"ticker": TICKER, "pct": 1.0, "direction": "up"}, expected,
                f"pct_chg={pct_chg} >= 1.0")

    # volume_spike: volume_ratio vs multiplier
    ratio_20d = tech.get("volume_ratio_20d", 1.0)
    expected = ratio_20d >= 1.5  # direction=expand, multiplier=1.5
    await check("volume_spike", {"ticker": TICKER, "direction": "expand", "multiplier": 1.5}, expected,
                f"volume_ratio_20d={ratio_20d} >= 1.5")

    # consecutive_days
    hist = market.get_price_history(TICKER, None, None)
    if hist.get("count", 0) >= 4:
        closes = [float(d["close"]) for d in hist["data"][-4:]]
        all_up = all(closes[i] > closes[i-1] for i in range(1, len(closes)))
        await check("consecutive_days", {"ticker": TICKER, "n": 3, "direction": "up"}, all_up,
                    f"last 4 closes: {[round(c,2) for c in closes]}")

    # turnover_rate
    turnover = market.get_turnover_rate(TICKER)
    if turnover is not None:
        expected = turnover >= 1.0  # pct=1.0, relation=above
        await check("turnover_rate", {"ticker": TICKER, "pct": 1.0, "relation": "above"}, expected,
                    f"turnover={turnover} >= 1.0")

    # open_gap: (open - prev_close) / prev_close
    prev_close = tech.get("prev_close")
    open_price = price_data.get("open", 0)
    if prev_close and prev_close != 0 and open_price != 0:
        gap_pct = (open_price - prev_close) / prev_close * 100
        expected = gap_pct >= 1.0  # direction=up, pct=1.0
        await check("open_gap", {"ticker": TICKER, "pct": 1.0, "direction": "up"}, expected,
                    f"gap_pct={gap_pct:.2f}% open={open_price} prev_close={prev_close}")

    # intraday_amplitude: high_pct - low_pct
    amplitude = intraday["high_pct"] - intraday["low_pct"]
    expected = amplitude >= 1.0  # pct=1.0, relation=above
    await check("intraday_amplitude", {"ticker": TICKER, "pct": 1.0, "relation": "above"}, expected,
                f"amplitude={amplitude:.2f}% (high_pct={intraday['high_pct']} - low_pct={intraday['low_pct']})")

    # ═══ 技术指标 ═══
    logger.info("\n── 技术指标 ──")

    # ma_position: price vs MA
    pos = tech.get("ma_position", {}).get("price_vs_MA20", "below")
    expected = pos == "below"  # relation=below
    await check("ma_position", {"ticker": TICKER, "ma": "MA20", "relation": "below"}, expected,
                f"pos={pos} price={price} MA20={ma20}")

    # ma_cross: MA5 vs MA20 golden/death
    ma5 = tech.get("ma", {}).get("MA5")
    prev_ma5 = tech.get("prev_ma", {}).get("MA5")
    prev_ma20 = tech.get("prev_ma", {}).get("MA20")
    if all(v is not None for v in [ma5, ma20, prev_ma5, prev_ma20]):
        is_golden = ma5 > ma20 and prev_ma5 <= prev_ma20
        await check("ma_cross", {"ticker": TICKER, "fast": "MA5", "slow": "MA20", "direction": "golden"},
                    is_golden, f"MA5={ma5} MA20={ma20} prev_MA5={prev_ma5} prev_MA20={prev_ma20}")

    # macd
    macd_data = tech.get("macd", {})
    hist_val = macd_data.get("hist", 0)
    prev_hist = macd_data.get("prev_hist", 0)
    if hist_val is not None and prev_hist is not None:
        golden = hist_val > 0 and prev_hist <= 0
        await check("macd", {"ticker": TICKER, "signal": "golden_cross"}, golden,
                    f"hist={hist_val} prev_hist={prev_hist}")

    # rsi: above/below threshold
    rsi_val = tech.get("rsi_14")
    if rsi_val is not None:
        expected = rsi_val <= 50  # relation=below, value=50
        await check("rsi", {"ticker": TICKER, "value": 50, "relation": "below"}, expected,
                    f"rsi={rsi_val} <= 50")

    # bollinger: position
    bb_pos = tech.get("bollinger", {}).get("position", "inside")
    expected = bb_pos == "inside"  # 测试用 inside
    await check("bollinger", {"ticker": TICKER, "position": "inside"}, expected,
                f"bb_position={bb_pos}")

    # kdj: golden_cross
    kdj_data = tech.get("kdj", {})
    if kdj_data and "prev_k" in kdj_data and "prev_d" in kdj_data:
        k = kdj_data["k"]
        d = kdj_data["d"]
        j = kdj_data["j"]
        prev_k = kdj_data["prev_k"]
        prev_d = kdj_data["prev_d"]
        kdj_golden = prev_k <= prev_d and k > d
        await check("kdj", {"ticker": TICKER, "signal": "golden_cross"}, kdj_golden,
                    f"k={k} d={d} j={j} prev_k={prev_k} prev_d={prev_d}")

    # volume_ratio: N=20 uses cache, N=5 computes independently
    daily_for_vol = market.get_bars(TICKER, "1d")
    if daily_for_vol is not None and len(daily_for_vol) >= 6:
        latest_vol = float(daily_for_vol.iloc[-1]["volume"])
        avg_vol_5 = float(np.mean(daily_for_vol["volume"].values[-6:-1]))
        ratio_5 = round(latest_vol / avg_vol_5, 2) if avg_vol_5 > 0 else 1.0
        expected = ratio_5 >= 1.5
        await check("volume_ratio", {"ticker": TICKER, "n": 5, "multiplier": 1.5, "relation": "above"},
                    expected, f"ratio_5d={ratio_5} >= 1.5")

    # ═══ 日内分时形态 ═══
    logger.info("\n── 日内分时形态 ──")

    # intraday_shot_up_fall
    bars = intraday.get("bars", [])
    high_pct = intraday["high_pct"]
    latest_pct = intraday["latest_pct"]
    rise_pct = 3.0
    fall_back_pct = 50.0
    if bars and len(bars) >= 2:
        open_price = float(intraday["open"])
        high_idx, high_bar = max(enumerate(bars), key=lambda item: float(item[1]["high"]))
        high_pct = round((float(high_bar["high"]) - open_price) / open_price * 100, 2) if open_price else 0
    else:
        high_idx = -1
    if high_pct >= rise_pct and high_idx < len(bars) - 1:
        fall_amount = high_pct - latest_pct
        fall_ratio = (fall_amount / high_pct * 100) if high_pct > 0 else 0
        expected_suf = fall_ratio >= fall_back_pct
    else:
        expected_suf = False
    await check("intraday_shot_up_fall",
                {"ticker": TICKER, "rise_pct": rise_pct, "fall_back_pct": fall_back_pct},
                expected_suf,
                f"high={high_pct}% latest={latest_pct}% fall_ratio={round((high_pct-latest_pct)/high_pct*100 if high_pct>0 else 0,1)}%")

    # intraday_dip_recover
    low_pct = intraday["low_pct"]
    dip_pct = 3.0
    recover_pct = 50.0
    if bars and len(bars) >= 2:
        open_price = float(intraday["open"])
        low_idx, low_bar = min(enumerate(bars), key=lambda item: float(item[1]["low"]))
        low_pct = round((float(low_bar["low"]) - open_price) / open_price * 100, 2) if open_price else 0
    else:
        low_idx = -1
    if low_pct <= -dip_pct and low_idx < len(bars) - 1:
        recovered = latest_pct - low_pct
        total_dip = abs(low_pct)
        recovery_ratio = (recovered / total_dip * 100) if total_dip > 0 else 0
        expected_dr = recovery_ratio >= recover_pct
    else:
        expected_dr = False
    await check("intraday_dip_recover",
                {"ticker": TICKER, "dip_pct": dip_pct, "recover_pct": recover_pct},
                expected_dr,
                f"low={low_pct}% latest={latest_pct}%")

    # intraday_A_shape
    if high_pct >= 2.0 and high_idx < len(bars) - 1:
        expected_A = abs(latest_pct) <= 0.5
    else:
        expected_A = False
    await check("intraday_A_shape",
                {"ticker": TICKER, "rise_pct": 2.0, "return_pct": 0.5},
                expected_A,
                f"high={high_pct}% latest={latest_pct}%")

    # intraday_V_shape
    if low_pct <= -2.0 and low_idx < len(bars) - 1:
        expected_V = abs(latest_pct) <= 0.5
    else:
        expected_V = False
    await check("intraday_V_shape",
                {"ticker": TICKER, "dip_pct": 2.0, "return_pct": 0.5},
                expected_V,
                f"low={low_pct}% latest={latest_pct}%")

    # intraday_trend
    duration_minutes = 5
    if bars and len(bars) >= duration_minutes + 1:
        recent = bars[-(duration_minutes + 1):]
        start_close = float(recent[0]["close"])
        end_close = float(recent[-1]["close"])
        window_pct = round((end_close - start_close) / start_close * 100, 2) if start_close else 0
        changes = [float(recent[i]["close"]) - float(recent[i - 1]["close"]) for i in range(1, len(recent))]
        expected_trend_up = all(change >= 0 for change in changes) and window_pct >= 1.0
    else:
        expected_trend_up = False
    await check("intraday_trend",
                {"ticker": TICKER, "direction": "up", "duration_minutes": duration_minutes, "pct": 1.0},
                expected_trend_up,
                f"window_pct={window_pct if bars and len(bars) >= duration_minutes + 1 else 'NA'}")

    # ═══ 市场情绪 ═══
    logger.info("\n── 市场情绪与资金 ──")

    # market_breadth: up_down_ratio >= threshold
    breadth = await market.get_market_breadth()
    ud_ratio = breadth.get("up_down_ratio", 1.0)
    expected_breadth = ud_ratio >= 1.0
    await check("market_breadth", {"up_down_ratio_min": 1.0}, expected_breadth,
                f"up_down_ratio={ud_ratio}")

    # market_volume: total_amount_yi vs target
    summary = market.get_today_market_summary()
    amount_yi = summary.get("total_amount_yi", 0) or 0
    expected_vol = amount_yi >= 5000
    await check("market_volume", {"amount_yi": 5000, "relation": "above"}, expected_vol,
                f"amount_yi={amount_yi} >= 5000")

    # sector_index_change
    overview = await market.get_sector_overview(SECTOR)
    sector_pct = overview.get("pct_chg", 0)
    expected_sec = sector_pct >= 1.0  # direction=up, pct=1.0
    await check("sector_index_change",
                {"sector": SECTOR, "pct": 1.0, "direction": "up"}, expected_sec,
                f"sector_pct_chg={sector_pct}%")

    # sector_breadth: up_count / (up+down) >= threshold
    up_count = overview.get("up_count", 0)
    down_count = overview.get("down_count", 0)
    total_ud = up_count + down_count
    up_ratio = up_count / total_ud if total_ud > 0 else 0
    expected_sec_br = up_ratio >= 0.5
    await check("sector_breadth", {"sector": SECTOR, "up_ratio_min": 0.5}, expected_sec_br,
                f"up={up_count} down={down_count} ratio={up_ratio:.2f}")

    # sector_volume_ratio
    vol_data = market.get_sector_volume_ratio(SECTOR_CODE)
    if "error" not in vol_data:
        sec_vol_ratio = vol_data["ratio"]
        expected_sec_vol = sec_vol_ratio >= 1.5
        await check("sector_volume_ratio", {"sector": SECTOR, "multiplier": 1.5}, expected_sec_vol,
                    f"sector_amount_ratio={sec_vol_ratio}")

    # sector_up_down_ratio: 成分股冲高回落占比
    members = market.get_concept_members(SECTOR_CODE)
    match_count = 0
    evaluated = 0
    for ticker in members:
        result = await eval_intraday_shot_up_fall({"ticker": ticker, "rise_pct": 3.0, "fall_back_pct": 50.0}, market)
        if "reason" not in result or "数据不足" not in str(result.get("reason", "")):
            evaluated += 1
        if result.get("triggered"):
            match_count += 1
    sec_pattern_ratio = (match_count / evaluated) if evaluated > 0 else 0
    expected_sec_pattern = sec_pattern_ratio >= 0.3
    await check("sector_up_down_ratio",
                {"sector": SECTOR, "pattern": "shot_up_fall", "ratio_min": 0.3},
                expected_sec_pattern,
                f"match={match_count} evaluated={evaluated} ratio={sec_pattern_ratio:.2f}")

    # sector_leader_strength
    leader_data = market.get_sector_leader(SECTOR_CODE)
    if "error" not in leader_data:
        leader_pct = leader_data["leader_pct_chg"]
        expected_leader = leader_pct >= 5.0
        await check("sector_leader_strength",
                    {"sector": SECTOR, "strength_pct": 5.0}, expected_leader,
                    f"leader_pct={leader_pct}% leader={leader_data['leader_ticker']}")

    # leader_divergence: |leader_pct - follow_avg| >= threshold
    leader_price = get_price_data(TICKER, market)
    leader_pct_val = leader_price.get("pct_chg", 0) or 0
    followers = [ticker for ticker in members if ticker != TICKER]
    follower_pcts: list[float] = []
    for ticker in followers:
        follower_price = get_price_data(ticker, market)
        pct_val = follower_price.get("pct_chg")
        if pct_val is not None:
            follower_pcts.append(float(pct_val))
    if follower_pcts:
        sector_avg = sum(follower_pcts) / len(follower_pcts)
        deviation = abs(leader_pct_val - sector_avg)
        expected_div = deviation >= 3.0
        await check("leader_divergence",
                    {"leader_ticker": TICKER, "sector": SECTOR, "pct": 3.0}, expected_div,
                    f"leader={leader_pct_val}% follow_avg={sector_avg:.2f}% deviation={deviation:.2f}%")

    # sector_relative_strength
    overview_b = await market.get_sector_overview("物联网")
    pct_a = overview.get("pct_chg", 0)
    pct_b = overview_b.get("pct_chg", 0)
    diff = pct_a - pct_b
    expected_rel = diff >= 1.0  # relation=above, pct=1.0
    await check("sector_relative_strength",
                {"sector_a": SECTOR, "sector_b": "物联网", "pct": 1.0, "relation": "above"},
                expected_rel, f"diff={diff:.2f}% ({SECTOR}={pct_a}% - 物联网={pct_b}%)")

    # sector_index_velocity (include_bars=True path)
    intraday_sec = market.get_sector_intraday(SECTOR_CODE, include_bars=True)
    if "error" not in intraday_sec and "bars" in intraday_sec:
        bars = intraday_sec["bars"]
        if len(bars) >= 6:
            start_close = bars[-6]["close"]
            end_close = bars[-1]["close"]
            velocity_5m = round((end_close - start_close) / start_close * 100, 2) if start_close else 0
            expected_vel = abs(velocity_5m) >= 1.0
            await check("sector_index_velocity",
                        {"sector": SECTOR, "pct": 1.0, "minutes": 5},
                        expected_vel,
                        f"velocity_5m={velocity_5m}% start={start_close} end={end_close}")

    # ═══ 时间 ═══
    logger.info("\n── 时间 ──")

    created = datetime(2026, 5, 23, 0, 0, 0)
    now = CLOCK.now

    # time_after: now >= created + days
    target = created + timedelta(days=3)  # 2026-05-26
    expected_ta = now >= target  # 2026-05-25 < 2026-05-26 → False
    await check("time_after", {"created_at": "2026-05-23T00:00:00", "now": now.isoformat(), "days": 3},
                expected_ta, f"target={target.isoformat()} now={now.isoformat()}")

    # time_window: days_min <= delta <= days_max
    start = created + timedelta(days=3)   # 2026-05-26
    end = created + timedelta(days=10)    # 2026-06-02
    expected_tw = start <= now <= end     # 2026-05-25 < start → False
    await check("time_window",
                {"created_at": "2026-05-23T00:00:00", "now": now.isoformat(), "days_min": 3, "days_max": 10},
                expected_tw, f"window=[{start.date()}, {end.date()}] now={now.date()}")

    # time_before: now <= created + days
    end_tb = created + timedelta(days=3)  # 2026-05-26
    expected_tb = now <= end_tb            # 2026-05-25 <= 2026-05-26 → True
    await check("time_before", {"created_at": "2026-05-23T00:00:00", "now": now.isoformat(), "days": 3},
                expected_tb, f"end={end_tb.date()} now={now.date()}")

    # ═══ 汇总 ═══
    passed = sum(1 for r in results if r["passed"])
    failed = [r for r in results if not r["passed"]]
    logger.info(f"\n{'=' * 70}")
    logger.info(f"  结果: {passed}/{len(results)} passed")
    if failed:
        logger.info(f"  FAILED: {len(failed)}")
        for r in failed:
            logger.info(f"    ✗ {r['name']}: atom={r['atom']} expected={r['expected']}")
    else:
        logger.info(f"  全部正确 ✓")
    logger.info(f"{'=' * 70}")


async def main():
    market = MarketDataProvider(clock=CLOCK)
    logger.info(f"clock.now={CLOCK.now}  _xt_ready={market._xt_ready}")
    await verify_all(market)

if __name__ == "__main__":
    asyncio.run(main())
