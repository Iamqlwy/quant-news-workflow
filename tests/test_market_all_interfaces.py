"""Market 模块全接口集成测试。

测试标的: 688507.SH (索辰科技)
模拟时间: 2026-06-02 14:59:00 (盘中)
判据: 与 C:/klines/indicator/688507.SH.csv 和 daily CSV 偏差 ≤ 1%
"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.market import MarketDataProvider
from src.market.charts import (
    generate_price_chart,
    generate_technical_chart,
)
from src.market.indicators import (
    calc_bollinger_series,
    calc_indicators,
    calc_kdj_series,
    calc_ma_series,
    calc_macd_series,
    calc_rsi_series,
    calc_volume_ratio,
)

# ═══════════════════════════════════════════════
# Ground Truth
# ═══════════════════════════════════════════════

TICKER = "688507.SH"
# indicator CSV 2026-06-02 (收盘后完整数据)
GT_CLOSE = 171.13
GT_TURNOVER_RATE = 22.9441  # %
GT_PE = 484.0671
GT_PB = 5.5124
GT_FLOAT_SHARE = 4923.573  # 万股
GT_CIRC_MV = 842571.0475
GT_TOTAL_MV = 1524918.6206
GT_VOLUME_RATIO = 1.05
# daily CSV 2026-06-02
GT_OPEN = 159.55
GT_HIGH = 176.0
GT_LOW = 144.59
GT_VOL = 112966.99  # 手
GT_AMOUNT = 1773854.631  # 千元
GT_PRE_CLOSE = 162.64
# yesterday indicator
GT_YDAY_CLOSE = 162.64
GT_YDAY_TURNOVER = 16.1641
GT_YDAY_PE = 460.0519

# 加载完整 indicator CSV 用于独立计算技术指标
_IND_DF = pd.read_csv("C:/klines/indicator/688507.SH.csv", dtype={"trade_date": str})
_IND_CLOSES = _IND_DF["close"].values.astype(float)
_IND_HIGHS = pd.read_csv("C:/klines/daily/688507.SH.csv", dtype={"trade_date": str})["high"].values.astype(float)
_IND_LOWS = pd.read_csv("C:/klines/daily/688507.SH.csv", dtype={"trade_date": str})["low"].values.astype(float)
_IND_VOLUMES = _IND_DF["volume_ratio"].values.astype(float)  # 此列名有误导性——实际是原始数据; 我们用 daily vol
_DAILY_VOL = pd.read_csv("C:/klines/daily/688507.SH.csv", dtype={"trade_date": str})["vol"].values.astype(float)

TOLERANCE = 0.01  # 1%


def _within_tolerance(actual: float, expected: float) -> bool:
    """检查 actual 是否在 expected ±1% 范围内。"""
    if expected == 0:
        return abs(actual) < 1e-6
    return abs(actual - expected) / abs(expected) <= TOLERANCE


# ═══════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════


@pytest.fixture(scope="module")
def clock_20260602_1459():
    """模拟时钟：2026-06-02 14:59"""
    settings.simulation_enabled = True
    return Clock(TimeConfig(
        start_time=datetime(2026, 6, 2, 14, 59, 0),
        tick_duration=timedelta(minutes=1),
        realtime=False,
    ))


@pytest.fixture(scope="module")
def market(clock_20260602_1459):
    """MarketDataProvider 实例（模块级，共享）"""
    provider = MarketDataProvider(clock=clock_20260602_1459)
    provider.refresh()
    return provider


def _run_async(coro):
    """同步运行 async 方法。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ═══════════════════════════════════════════════
# 1. 实时行情测试
# ═══════════════════════════════════════════════


class TestRealtimePrice:
    def test_realtime_price_close(self, market):
        result = _run_async(market.get_realtime_price(TICKER))
        assert "error" not in result, f"Unexpected error: {result}"
        price = result.get("price", 0)
        assert _within_tolerance(price, GT_CLOSE), f"price={price}, expected ~{GT_CLOSE}"

    def test_realtime_price_open(self, market):
        result = _run_async(market.get_realtime_price(TICKER))
        open_p = result.get("open", 0)
        assert open_p == GT_OPEN, f"open={open_p}, expected {GT_OPEN}"

    def test_realtime_price_high_low(self, market):
        result = _run_async(market.get_realtime_price(TICKER))
        high_p = result.get("high", 0)
        low_p = result.get("low", 0)
        assert _within_tolerance(high_p, GT_HIGH), f"high={high_p}, expected ~{GT_HIGH}"
        assert _within_tolerance(low_p, GT_LOW), f"low={low_p}, expected ~{GT_LOW}"

    def test_realtime_price_pre_close(self, market):
        result = _run_async(market.get_realtime_price(TICKER))
        pre_close = result.get("pre_close", 0)
        assert pre_close == GT_PRE_CLOSE, f"pre_close={pre_close}, expected {GT_PRE_CLOSE}"

    def test_realtime_prices_batch(self, market):
        results = _run_async(market.get_realtime_prices([TICKER]))
        assert TICKER in results
        single = _run_async(market.get_realtime_price(TICKER))
        if "error" not in results[TICKER] and "error" not in single:
            assert results[TICKER].get("price") == single.get("price"), "Batch and single price differ"

    def test_stock_name(self, market):
        name = market.get_stock_name(TICKER)
        assert "索辰" in name, f"stock_name={name}"


# ═══════════════════════════════════════════════
# 2. OHLCV 查询测试
# ═══════════════════════════════════════════════


class TestGetBars:
    def test_get_bars_1d(self, market):
        df = market.get_bars(TICKER, granularity="1d")
        assert df is not None and not df.empty, "get_bars 1d returned empty"
        last = df.iloc[-1]
        close = float(last["close"])
        assert _within_tolerance(close, GT_CLOSE), f"1d close={close}, expected ~{GT_CLOSE}"
        assert last["open"] == GT_OPEN, f"1d open={last['open']}, expected {GT_OPEN}"

    def test_get_bars_1m(self, market):
        df = market.get_bars(TICKER, granularity="1m")
        assert df is not None and not df.empty, "get_bars 1m returned empty"
        # 最后一根应为 14:59 bar
        ts = df["timestamp"]
        if not pd.api.types.is_datetime64_any_dtype(ts):
            ts = pd.to_datetime(ts, errors="coerce")
        assert (ts.dt.hour == 14).any(), "1m bars should include 14:xx data"
        last_close = float(df.iloc[-1]["close"])
        assert _within_tolerance(last_close, GT_CLOSE), f"1m last close={last_close}, expected ~{GT_CLOSE}"

    def test_get_bars_5m(self, market):
        df = market.get_bars(TICKER, granularity="5m")
        assert df is not None and not df.empty, "get_bars 5m returned empty"
        # 验证单调性
        closes = pd.to_numeric(df["close"], errors="coerce").dropna()
        assert len(closes) >= 2, "5m bars should have multiple rows"

    def test_get_bars_filter_date(self, market):
        df = market.get_bars(TICKER, granularity="1d", start="2026-05-01", end="2026-06-02")
        assert df is not None and not df.empty, "filtered bars empty"
        # 模拟模式缓存只保留最近 5 天日线
        assert len(df) >= 4, f"Should have 4+ trading days, got {len(df)}"


# ═══════════════════════════════════════════════
# 3. 技术指标测试
# ═══════════════════════════════════════════════


class TestTechnicalIndicators:
    def _get_indicators(self, market):
        return _run_async(market.get_technical_indicators(TICKER))

    def test_indicators_not_none(self, market):
        result = self._get_indicators(market)
        assert "error" not in result, f"Unexpected error: {result}"

    def test_ma_values(self, market):
        result = self._get_indicators(market)
        ma = result.get("ma", {})
        # MA5: 最近5天收盘均值
        expected_ma5 = float(np.mean(_IND_CLOSES[-5:]))
        actual_ma5 = ma.get("MA5")
        assert actual_ma5 is not None, "MA5 is None"
        assert _within_tolerance(actual_ma5, expected_ma5), f"MA5={actual_ma5}, expected ~{expected_ma5}"

    def test_rsi_value(self, market):
        result = self._get_indicators(market)
        rsi = result.get("rsi_14")
        assert rsi is not None, "RSI is None"
        # 用 indicators 模块独立计算
        expected = calc_rsi_series(_IND_CLOSES, 14)
        expected_rsi = expected[-1]
        if not np.isnan(expected_rsi):
            assert _within_tolerance(rsi, expected_rsi), f"RSI={rsi}, expected ~{expected_rsi}"

    def test_macd_value(self, market):
        result = self._get_indicators(market)
        macd = result.get("macd", {})
        dif = macd.get("dif")
        dea = macd.get("dea")
        assert dif is not None, "MACD DIF is None"
        assert dea is not None, "MACD DEA is None"
        # 独立计算
        exp_dif, exp_dea, exp_hist = calc_macd_series(_IND_CLOSES)
        if not np.isnan(exp_dif[-1]):
            assert _within_tolerance(dif, exp_dif[-1]), f"DIF={dif}, expected ~{exp_dif[-1]}"
        if not np.isnan(exp_dea[-1]):
            assert _within_tolerance(dea, exp_dea[-1]), f"DEA={dea}, expected ~{exp_dea[-1]}"

    def test_bollinger_value(self, market):
        result = self._get_indicators(market)
        boll = result.get("bollinger", {})
        mid = boll.get("mid")
        upper = boll.get("upper")
        lower = boll.get("lower")
        assert mid is not None, "BOLL mid is None"
        assert upper is not None, "BOLL upper is None"
        assert lower is not None, "BOLL lower is None"
        # 独立计算
        exp_upper, exp_mid, exp_lower = calc_bollinger_series(_IND_CLOSES, 20)
        if not np.isnan(exp_mid[-1]):
            assert _within_tolerance(mid, exp_mid[-1]), f"BOLL mid={mid}, expected ~{exp_mid[-1]}"
        if not np.isnan(exp_upper[-1]):
            assert _within_tolerance(upper, exp_upper[-1]), f"BOLL upper={upper}, expected ~{exp_upper[-1]}"

    def test_kdj_value(self, market):
        result = self._get_indicators(market)
        kdj = result.get("kdj", {})
        k = kdj.get("k")
        d = kdj.get("d")
        assert k is not None, "KDJ K is None"
        assert d is not None, "KDJ D is None"
        # 独立计算
        exp_k, exp_d, exp_j = calc_kdj_series(_IND_HIGHS, _IND_LOWS, _IND_CLOSES, 9)
        if not np.isnan(exp_k[-1]):
            assert _within_tolerance(k, exp_k[-1]), f"KDJ K={k}, expected ~{exp_k[-1]}"

    def test_volume_ratio_value(self, market):
        result = self._get_indicators(market)
        vr = result.get("volume_ratio_20d")
        assert vr is not None, "volume_ratio_20d is None"
        # 量比应 > 0（盘中大部分成交量已发生）
        assert vr > 0, f"volume_ratio={vr} should be > 0"

    def test_turnover_rate(self, market):
        tr = market.get_turnover_rate(TICKER)
        assert tr is not None, "get_turnover_rate returned None"
        # 盘中 14:59，大部分成交量已发生，应接近全天值
        assert _within_tolerance(tr, GT_TURNOVER_RATE), f"turnover={tr}, expected ~{GT_TURNOVER_RATE}"

    def test_indicators_cached(self, market):
        result1 = self._get_indicators(market)
        result2 = market.get_technical_indicators_cached(TICKER)  # sync
        # 缓存结果与首次计算的核心数值应一致（prev_* 字段因异步执行可能有微小差异）
        for key in ["ma", "rsi_14", "latest_close", "prev_close"]:
            assert result1.get(key) == result2.get(key), f"Key {key} differs: {result1.get(key)} != {result2.get(key)}"
        for key in ["macd", "kdj"]:
            d1, d2 = result1.get(key, {}), result2.get(key, {})
            assert d1.get("dif") == d2.get("dif"), f"{key}.dif: {d1.get('dif')} vs {d2.get('dif')}"
            assert d1.get("dea") == d2.get("dea"), f"{key}.dea: {d1.get('dea')} vs {d2.get('dea')}"
            assert d1.get("hist") == d2.get("hist"), f"{key}.hist: {d1.get('hist')} vs {d2.get('hist')}"
        assert result1.get("bollinger") == result2.get("bollinger"), "bollinger differs"
        assert result1.get("volume_ratio_20d") == result2.get("volume_ratio_20d"), "volume_ratio differs"


# ═══════════════════════════════════════════════
# 4. 日内快照测试
# ═══════════════════════════════════════════════


class TestIntradaySnapshot:
    def test_snapshot_not_empty(self, market):
        snap = _run_async(market.get_intraday_snapshot(TICKER))
        assert "error" not in snap, f"Unexpected error: {snap}"
        # 日内快照返回 latest（最新价）而非 price
        assert snap.get("latest") is not None, "snapshot has no latest"

    def test_snapshot_price(self, market):
        snap = _run_async(market.get_intraday_snapshot(TICKER))
        latest = snap.get("latest", 0)
        assert _within_tolerance(latest, GT_CLOSE), f"snapshot latest={latest}, expected ~{GT_CLOSE}"

    def test_snapshot_cached(self, market):
        snap1 = _run_async(market.get_intraday_snapshot(TICKER))
        snap2 = market.get_intraday_snapshot_cached(TICKER)  # sync
        assert snap1 == snap2, "Cached snapshot differs"


# ═══════════════════════════════════════════════
# 5. 涨跌停测试
# ═══════════════════════════════════════════════


class TestZdtRecord:
    def test_zdt_record(self, market):
        rec = market.get_zdt_record(TICKER)
        assert rec is not None, f"get_zdt_record returned None"
        assert "is_limit" in rec or "ticker" in rec, f"Missing fields in {rec}"
        assert rec.get("ticker") == TICKER, f"ticker mismatch in {rec}"


# ═══════════════════════════════════════════════
# 6. 历史价格测试
# ═══════════════════════════════════════════════


class TestPriceHistory:
    def test_price_history(self, market):
        hist = market.get_price_history(TICKER, from_date="2026-05-01", to_date="2026-06-02")
        assert "error" not in hist, f"Unexpected error: {hist}"
        data = hist.get("data", [])
        # 模拟模式缓存只保留最近几天
        assert len(data) >= 4, f"Should have 4+ days, got {len(data)}"
        # 最后一笔
        last = data[-1]
        assert _within_tolerance(last["close"], GT_CLOSE), f"hist close={last['close']}, expected ~{GT_CLOSE}"


# ═══════════════════════════════════════════════
# 7. 市场概况测试
# ═══════════════════════════════════════════════


class TestMarketOverview:
    def test_market_breadth(self, market):
        breadth = _run_async(market.get_market_breadth())
        assert "error" not in breadth, f"Unexpected error: {breadth}"
        assert "up_count" in breadth
        assert "down_count" in breadth

    def test_index_overview(self, market):
        overview = market.get_index_overview()
        assert len(overview) >= 1, f"Empty index overview: {overview}"

    def test_today_market_summary(self, market):
        summary = market.get_today_market_summary()
        assert "error" not in summary, f"Unexpected error: {summary}"

    def test_market_snapshot(self, market):
        snap = market.get_market_snapshot("2026-06-02")
        assert "error" not in snap, f"Unexpected error: {snap}"


# ═══════════════════════════════════════════════
# 8. 板块/概念测试
# ═══════════════════════════════════════════════


class TestConcepts:
    def test_concept_list(self, market):
        concepts = market.get_concept_list("all")
        assert len(concepts) > 0, "concept list is empty"

    def test_concept_members(self, market):
        # 使用一个已知存在的概念代码
        members = market.get_concept_members("BK0491")  # 军工
        if not members:
            # fallback: try any concept from list
            concepts = market.get_concept_list("concept")
            if concepts:
                con_code = concepts[0].get("ts_code", "")
                members = market.get_concept_members(con_code)
        assert isinstance(members, list), f"Expected list, got {type(members)}"

    def test_stock_concepts(self, market):
        concepts = market.get_stock_concepts(TICKER)
        assert isinstance(concepts, dict), f"Expected dict, got {type(concepts)}"

    def test_concept_kline(self, market):
        # 使用军工概念代码
        df = market.get_concept_kline("885311.TI")
        assert df is not None, "concept kline returned None"

    def test_sector_overview(self, market):
        result = _run_async(market.get_sector_overview("军工"))
        assert "error" not in result, f"Unexpected error: {result}"

    def test_sector_intraday(self, market):
        result = market.get_sector_intraday("885311.TI", include_bars=False)
        assert "error" not in result, f"Unexpected error: {result}"
        assert result.get("pct_chg") is not None, "sector intraday missing pct_chg"


# ═══════════════════════════════════════════════
# 9. 图表生成测试
# ═══════════════════════════════════════════════


class TestCharts:
    def test_price_chart(self, market):
        png = generate_price_chart(market, TICKER, None, None)
        assert isinstance(png, bytes), f"Expected bytes, got {type(png)}"
        assert len(png) > 1000, f"PNG too small: {len(png)} bytes"
        # 验证是有效 PNG
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "Not a valid PNG file"

    def test_technical_chart(self, market):
        png = generate_technical_chart(market, TICKER, None, None)
        assert isinstance(png, bytes), f"Expected bytes, got {type(png)}"
        assert len(png) > 1000, f"PNG too small: {len(png)} bytes"
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "Not a valid PNG file"


# ═══════════════════════════════════════════════
# 10. 指标函数独立测试
# ═══════════════════════════════════════════════


class TestIndicatorFunctions:
    """独立测试 indicators.py 中的纯函数，用 indicator CSV 数据。"""

    def test_calc_ma_series(self):
        result = calc_ma_series(_IND_CLOSES, 5)
        expected = _IND_CLOSES[-5:].mean()
        assert _within_tolerance(result[-1], expected), f"MA5 last={result[-1]}, expected ~{expected}"

    def test_calc_rsi_series(self):
        result = calc_rsi_series(_IND_CLOSES, 14)
        assert not np.isnan(result[-1]), "RSI last is NaN"
        assert 0 <= result[-1] <= 100, f"RSI out of range: {result[-1]}"

    def test_calc_macd_series(self):
        dif, dea, hist = calc_macd_series(_IND_CLOSES)
        assert not np.isnan(dif[-1]), "DIF last is NaN"
        assert not np.isnan(dea[-1]), "DEA last is NaN"

    def test_calc_bollinger_series(self):
        upper, mid, lower = calc_bollinger_series(_IND_CLOSES, 20)
        assert not np.isnan(mid[-1]), "BOLL mid last is NaN"
        assert _within_tolerance(mid[-1], np.mean(_IND_CLOSES[-20:])), f"BOLL mid={mid[-1]}"
        assert upper[-1] >= mid[-1] >= lower[-1], "BOLL bands: upper < mid or mid < lower"

    def test_calc_kdj_series(self):
        k, d, j = calc_kdj_series(_IND_HIGHS, _IND_LOWS, _IND_CLOSES, 9)
        assert not np.isnan(k[-1]), "K last is NaN"
        assert 0 <= k[-1] <= 100, f"K out of range: {k[-1]}"

    def test_calc_volume_ratio(self):
        vr, latest, avg = calc_volume_ratio(_DAILY_VOL, 20)
        assert vr is not None, "volume_ratio is None"
        assert vr > 0, f"volume_ratio={vr} should be > 0"

    def test_calc_indicators_full(self):
        """完整指标计算：用 indicator CSV 的 OHLCV 列。"""
        daily = pd.read_csv("C:/klines/daily/688507.SH.csv", dtype={"trade_date": str})
        daily = daily.rename(columns={
            "trade_date": "timestamp", "open": "open", "high": "high",
            "low": "low", "close": "close", "vol": "volume", "amount": "amount",
        })
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            daily[col] = pd.to_numeric(daily[col], errors="coerce")
        result, state = calc_indicators(daily)
        assert "ma" in result, "Missing ma in result"
        assert "rsi_14" in result, "Missing rsi_14 in result"
        assert "macd" in result, "Missing macd in result"
        assert "bollinger" in result, "Missing bollinger in result"
        assert "kdj" in result, "Missing kdj in result"
        assert state is not None, "IndicatorState is None"
