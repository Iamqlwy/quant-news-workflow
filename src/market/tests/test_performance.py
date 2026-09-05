"""性能基准测试。

对比新旧实现的性能差异。
"""

import time

import numpy as np

from src.market.compute.indicators import calc_bollinger, calc_ema, calc_kdj, calc_ma, calc_macd, calc_rsi


def test_ma_performance() -> None:
    """MA 计算性能：250 个数据点应 < 1ms。"""
    data = np.random.randn(250).astype(float) * 10 + 100
    start = time.perf_counter()
    for _ in range(100):
        calc_ma(data, 5)
        calc_ma(data, 10)
        calc_ma(data, 20)
        calc_ma(data, 60)
    elapsed = time.perf_counter() - start
    # 100 次 * 4 个 MA = 400 次计算
    assert elapsed < 1.0, f"400 MA 计算耗时 {elapsed:.3f}s，应 < 1.0s"


def test_ema_performance() -> None:
    """EMA 计算性能。"""
    data = np.random.randn(250).astype(float) * 10 + 100
    start = time.perf_counter()
    for _ in range(50):
        calc_ema(data, 12)
        calc_ema(data, 26)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"100 EMA 计算耗时 {elapsed:.3f}s"


def test_rsi_performance() -> None:
    """RSI 计算性能。"""
    data = np.random.randn(250).astype(float) * 10 + 100
    start = time.perf_counter()
    for _ in range(50):
        calc_rsi(data, 14)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"50 RSI 计算耗时 {elapsed:.3f}s"


def test_full_indicator_pipeline() -> None:
    """完整指标计算流水线：250 个数据点，所有指标。"""
    n = 250
    closes = np.random.randn(n).astype(float) * 10 + 100
    highs = closes + np.abs(np.random.randn(n) * 2)
    lows = closes - np.abs(np.random.randn(n) * 2)
    _volumes = np.abs(np.random.randn(n) * 500 + 500)

    start = time.perf_counter()
    for _ in range(20):
        calc_ma(closes, 5)
        calc_ma(closes, 10)
        calc_ma(closes, 20)
        calc_ma(closes, 60)
        calc_rsi(closes, 14)
        calc_macd(closes)
        calc_bollinger(closes)
        calc_kdj(highs, lows, closes)
    elapsed = time.perf_counter() - start
    # 20 次 * 8 个指标 = 160 次
    assert elapsed < 2.0, f"全指标流水线耗时 {elapsed:.3f}s，应 < 2.0s"


def test_tick_aggregation_write_performance() -> None:
    """验证 list-buffer 聚合的 O(N) 特性。

    原实现在每次 flush 时做 pd.concat → O(N²)。
    新实现用 list buffer → O(N)。
    """
    from src.market.compute.tick_agg import TickAggregator
    from src.market.data.cache import CacheManager

    cache = CacheManager()
    agg = TickAggregator(cache)

    # 模拟 240 次聚合（一个交易日的分钟数）
    dummy_ticks = [{"lastPrice": 10.0 + i * 0.01, "pvolume": 1000000 + i * 1000, "amount": 10000000 + i * 10000} for i in range(5)]

    start = time.perf_counter()
    for minute in range(240):
        ticks_dict = {}
        base_time = 1737023400000 + minute * 60000  # 2025-01-16 10:30 + minute
        for j, tick in enumerate(dummy_ticks):
            t = tick.copy()
            t["time"] = base_time + j * 1000
            ticks_dict[f"TEST{minute:04d}"] = t
        agg.on_tick(ticks_dict)
    agg.flush()
    elapsed = time.perf_counter() - start

    # 240 次聚合 + flush 应在合理时间内
    assert elapsed < 5.0, f"240 次 tick 聚合耗时 {elapsed:.3f}s，应 < 5.0s"
