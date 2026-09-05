"""扫描冲高回落股票 + atom 验证 —— 时钟固定在 2026-05-26 11:00"""
from __future__ import annotations
from loguru import logger

import asyncio
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market.data import MarketDataProvider
from src.triggers.evaluators import evaluate_atom

settings.simulation_enabled = True

KLINES = Path(settings.klines_path)
CLOCK = Clock(TimeConfig(
    start_time=datetime(2026, 5, 26, 11, 0, 0),
    tick_duration=timedelta(minutes=5),
    realtime=False,
))
RISE_PCT = 3.0       # 最高涨幅 >= 3%
FALL_BACK_PCT = 50.0  # 回落 >= 最高涨幅的 50%


def scan_shot_up_fall(market: MarketDataProvider) -> list[dict]:
    """扫描全部有 1m 数据的股票，找出冲高回落形态"""
    results: list[dict] = []
    one_m_dir = KLINES / "1m"
    files = sorted(one_m_dir.glob("*.csv"))
    t0 = _time.time()

    for i, path in enumerate(files):
        ticker = path.stem
        snap = market._csv_intraday_snapshot(ticker)
        if "error" in snap:
            continue

        # 每 500 只清理 1m 缓存，避免内存爆炸
        if i > 0 and i % 500 == 0:
            market._1m_df_cache.clear()

        high_pct = snap["high_pct"]
        latest_pct = snap["latest_pct"]

        if high_pct < RISE_PCT:
            continue

        fall_amount = high_pct - latest_pct
        fall_ratio = (fall_amount / high_pct * 100) if high_pct > 0 else 0
        if fall_ratio >= FALL_BACK_PCT:
            results.append({
                "ticker": ticker,
                "open": snap["open"],
                "high_pct": high_pct,
                "latest_pct": latest_pct,
                "fall_ratio": round(fall_ratio, 1),
                "session_high": snap["session_high"],
                "session_low": snap["session_low"],
                "latest": snap["latest"],
                "bar_count": snap["count"],
            })

    elapsed = _time.time() - t0
    results.sort(key=lambda x: x["fall_ratio"], reverse=True)
    return results, elapsed


async def verify_with_atom(market: MarketDataProvider, candidates: list[dict], top_n: int = 15) -> list[dict]:
    """用 atom 评估器验证扫描结果"""
    verified: list[dict] = []
    for c in candidates[:top_n]:
        params = {"ticker": c["ticker"], "rise_pct": RISE_PCT, "fall_back_pct": FALL_BACK_PCT}
        result = await evaluate_atom("intraday_shot_up_fall", params, market)
        c["atom_triggered"] = result.get("triggered", False)
        c["atom_detail"] = result.get("reason", result.get("detail", ""))
        c["match"] = c["atom_triggered"] == (c["fall_ratio"] >= FALL_BACK_PCT)
        verified.append(c)
    return verified


async def main():
    logger.info("=" * 70)
    logger.info("  冲高回落 (Shot-Up-Fall) 扫描 + Atom 验证")
    logger.info(f"  时钟: {CLOCK.now}  |  rise>= {RISE_PCT}%  |  fall_back>= {FALL_BACK_PCT}%")
    logger.info("=" * 70)

    market = MarketDataProvider(clock=CLOCK)
    logger.info(f"  _xt_ready={market._xt_ready}  trading_days={len(market._cache.get('daily_window', []))}")
    logger.info()

    # ── 扫描 ──
    logger.info("扫描中...")
    candidates, elapsed = scan_shot_up_fall(market)
    logger.info(f"扫描完成: {len(candidates)} 只股票匹配, 耗时 {elapsed:.1f}s")
    logger.info()

    if not candidates:
        logger.info("未找到冲高回落股票")
        return

    # ── Top 15 ──
    logger.info(f"{'─' * 70}")
    logger.info(f"  Top 15 冲高回落 (按回落幅度降序)")
    logger.info(f"{'─' * 70}")
    logger.info(f"{'ticker':<14} {'high%':>7} {'now%':>7} {'fall%':>7} {'bars':>5}  {'open':>8} {'high':>8} {'latest':>8}")
    logger.info(f"{'─' * 70}")
    for c in candidates[:15]:
        logger.info(f"{c['ticker']:<14} {c['high_pct']:>+6.1f}% {c['latest_pct']:>+6.1f}% {c['fall_ratio']:>6.1f}% {c['bar_count']:>5}  {c['open']:>8.2f} {c['session_high']:>8.2f} {c['latest']:>8.2f}")

    # ── Atom 验证 ──
    logger.info(f"\n{'─' * 70}")
    logger.info(f"  Atom 验证 (前 {min(15, len(candidates))} 只)")
    logger.info(f"{'─' * 70}")
    verified = await verify_with_atom(market, candidates, top_n=15)
    all_match = True
    for c in verified:
        status = "✓" if c["match"] else "✗ MISMATCH"
        if not c["match"]:
            all_match = False
        logger.info(f"  {status} {c['ticker']:<14} scan={c['fall_ratio']}% fall_ratio>=50%→triggered  |  atom.triggered={c['atom_triggered']}  {c['atom_detail']}")

    if all_match:
        logger.info(f"\n  ✓ 全部一致 — 扫描结果与 atom 评估完全匹配")
    else:
        logger.info(f"\n  ✗ 存在不一致 — 需要排查")


if __name__ == "__main__":
    asyncio.run(main())
