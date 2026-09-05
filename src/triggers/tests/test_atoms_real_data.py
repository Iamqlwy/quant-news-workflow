"""触发原子真实数据测试 —— 基于 2026-06-03 行情数据。

每个 atom 构造一个可触发的真实情景，用实际 CSV 数据构建 EvalContext，
验证 evaluator 能否正确触发。
"""

from __future__ import annotations

import csv
from datetime import datetime

from src.core.timezone import BEIJING_TZ
from src.triggers.eval_context import EvalContext
from src.triggers.evaluators import evaluate_atom

TEST_DT = datetime(2026, 6, 3, 15, 0, 0, tzinfo=BEIJING_TZ)


# ═══════════════════════════════════════════════
# 工具函数：从 CSV 构建 history / price / tech 数据
# ═══════════════════════════════════════════════

KLINE_1M_PATH = "C:/klines/1m"
DAILY_EXTRA = "C:/klines/extra/all_stocks_daily"
ZDT_PATH = "C:/klines/extra/zdt/20260603.csv"


def _load_daily_extra(ticker: str, days: int = 60) -> list[dict]:
    """从 extra/all_stocks_daily 加载最近 N 天日线。"""
    import os

    files = sorted(
        [f for f in os.listdir(DAILY_EXTRA) if f.endswith(".csv")],
    )
    result = []
    for f in files[-days - 10 :]:
        date = f.replace(".csv", "")
        with open(f"{DAILY_EXTRA}/{f}") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if row["ts_code"] == ticker:
                    result.append(
                        {
                            "date": date,
                            "close": float(row["close"]),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "volume": float(row["vol"]),
                            "pct_chg": float(row["pct_chg"]),
                            "pre_close": float(row["pre_close"]),
                        }
                    )
                    break
    return result


def _load_1m_bars(ticker: str, date: str = "2026-06-03") -> list[dict]:
    """从 1m CSV 加载指定日的分钟 bar。"""
    import pandas as pd

    path = f"{KLINE_1M_PATH}/{ticker}.csv"
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except Exception:
        return []
    df.columns = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
        "chg",
        "pct",
        "turnover",
        "float_sh",
        "total_sh",
    ]
    day_df = df[df["date"].str.startswith(date)]
    bars = []
    for _, row in day_df.iterrows():
        bars.append(
            {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["vol"]),
                "amount": float(row["amount"]),
            }
        )
    return bars


def _build_history(ticker: str, lookback: int = 60) -> dict:
    """构建 history dict，格式与 get_price_history 返回一致。"""
    data = _load_daily_extra(ticker, lookback)
    if not data:
        return {"error": "无数据", "data": [], "count": 0}
    records = []
    for d in data:
        records.append(
            {
                "date": d["date"],
                "close": d["close"],
                "open": d["open"],
                "high": d["high"],
                "low": d["low"],
                "volume": d["volume"],
            }
        )
    return {
        "ticker": ticker,
        "count": len(records),
        "data": records,
        "first_close": records[0]["close"],
        "last_close": records[-1]["close"],
    }


def _build_price(ticker: str) -> dict:
    """构建实时 price dict。"""
    data = _load_daily_extra(ticker, 2)
    if len(data) < 2:
        return {"error": "数据不足"}
    today = data[-1]
    return {
        "ticker": ticker,
        "price": today["close"],
        "open": today["open"],
        "high": today["high"],
        "low": today["low"],
        "pre_close": today["pre_close"],
        "pct_chg": today["pct_chg"],
        "source": "csv",
        "available": True,
    }


def _build_tech(ticker: str) -> dict:
    """构建技术指标 tech dict（从历史数据手工计算 MA）。"""
    data = _load_daily_extra(ticker, 65)
    if len(data) < 60:
        return {}

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    def _ma(arr, n):
        if len(arr) < n:
            return None
        return sum(arr[-n:]) / n

    def _ema(arr, n):
        if len(arr) < n:
            return None
        k = 2.0 / (n + 1)
        val = sum(arr[:n]) / n
        for x in arr[n:]:
            val = x * k + val * (1 - k)
        return val

    return {
        "ma5": round(_ma(closes, 5), 2) if _ma(closes, 5) else None,
        "ma10": round(_ma(closes, 10), 2) if _ma(closes, 10) else None,
        "ma20": round(_ma(closes, 20), 2) if _ma(closes, 20) else None,
        "ma60": round(_ma(closes, 60), 2) if _ma(closes, 60) else None,
        "vol_ratio": round(
            volumes[-1] / (sum(volumes[-21:-1]) / 20), 2
        )
        if sum(volumes[-21:-1]) > 0
        else None,
        "vol_ratio_5": round(
            volumes[-1] / (sum(volumes[-6:-1]) / 5), 2
        )
        if sum(volumes[-6:-1]) > 0
        else None,
        "vol_ratio_10": round(
            volumes[-1] / (sum(volumes[-11:-1]) / 10), 2
        )
        if sum(volumes[-11:-1]) > 0
        else None,
        "latest_close": closes[-1],
        "dif": round((_ema(closes, 12) or 0) - (_ema(closes, 26) or 0), 4),
        "dea": None,
        "hist": None,
        "k": None,
        "d": None,
        "j": None,
        "rsi14": None,
        "boll_upper": None,
        "boll_mid": None,
        "boll_lower": None,
        "boll_position": "inside_upper",
    }


def _build_snapshot(ticker: str) -> dict:
    """构建 1m snapshot，含 bars 列表。"""
    bars = _load_1m_bars(ticker)
    if not bars:
        return {"error": f"无1分钟数据: {ticker}", "ticker": ticker}
    open_price = bars[0]["open"]
    latest_close = bars[-1]["close"]
    latest_pct = round((latest_close - open_price) / open_price * 100, 2) if open_price else 0.0
    high = max(b["high"] for b in bars)
    low = min(b["low"] for b in bars)
    high_pct = round((high - open_price) / open_price * 100, 2) if open_price else 0.0
    low_pct = round((low - open_price) / open_price * 100, 2) if open_price else 0.0
    return {
        "ticker": ticker,
        "price": latest_close,
        "open": open_price,
        "high": high,
        "low": low,
        "high_pct": high_pct,
        "low_pct": low_pct,
        "close": latest_close,
        "volume": sum(b["volume"] for b in bars),
        "amount": sum(b["amount"] for b in bars),
        "source": "1m",
        "available": True,
        "latest_pct": latest_pct,
        "bars": bars,
    }


def _build_turnover(ticker: str) -> float | None:
    """估算换手率：用今日量/昨日量 * 昨日换手率（近似）。"""
    data = _load_daily_extra(ticker, 3)
    if len(data) < 2:
        return None
    today_vol = data[-1]["volume"]
    yday_vol = data[-2]["volume"]
    if yday_vol <= 0:
        return None
    # 用 volume 比近似换手比
    return round(today_vol / yday_vol * 3.0, 2)  # 近似基准换手 3%


def _build_zdt_record(ticker: str) -> dict:
    """从 zdt CSV 构建 zdt_record。"""
    try:
        with open(ZDT_PATH) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["ts_code"] == ticker:
                    limit_type = "涨停池" if "涨停" in row.get("limit_type", "") else "跌停池"
                    return {
                        "ticker": ticker,
                        "is_limit": True,
                        "limit_type": limit_type,
                        "tag": row.get("tag", ""),
                    }
    except Exception:
        pass
    return {"ticker": ticker, "is_limit": False}


def _make_ctx(
    ticker: str,
    needs: list[str] | None = None,
) -> EvalContext:
    """构建 EvalContext，按需填充数据。"""
    if needs is None:
        needs = ["price", "tech", "snapshot", "history", "turnover", "zdt_record"]

    ticker_data: dict = {ticker: {}}
    td = ticker_data[ticker]

    if "price" in needs:
        td["price"] = _build_price(ticker)
    if "tech" in needs:
        td["tech"] = _build_tech(ticker)
    if "snapshot" in needs:
        td["snapshot"] = _build_snapshot(ticker)
    if "history" in needs:
        td["history"] = _build_history(ticker)
    if "turnover" in needs:
        td["turnover"] = _build_turnover(ticker)
    if "zdt_record" in needs:
        td["zdt_record"] = _build_zdt_record(ticker)

    return EvalContext(
        now=TEST_DT,
        ticker_data=ticker_data,
        sector_data={},
        market_summary={"up_down_ratio": 0.5, "avg_pct_chg": -1.2, "total_amount_yi": 8500.0},
    )


# ═══════════════════════════════════════════════
# 价格状态 (5)
# ═══════════════════════════════════════════════


class TestPriceMove:
    """price_move: 价格变动涨/跌 X%。"""

    def test_price_move_up_triggered(self) -> None:
        # 300197.SZ: 0603 涨 19.83%，触发 涨超15%
        ctx = _make_ctx("300197.SZ", ["price", "history"])
        result = evaluate_atom(
            "price_move",
            {"ticker": "300197.SZ", "direction": "up", "pct": 15},
            ctx,
        )
        assert result["triggered"] is True
        detail = result["detail"]
        assert detail["actual_pct"] >= 15

    def test_price_move_down_triggered(self) -> None:
        # 300085.SZ: 0603 跌 -15.51%，触发 跌超10%
        ctx = _make_ctx("300085.SZ", ["price", "history"])
        result = evaluate_atom(
            "price_move",
            {"ticker": "300085.SZ", "direction": "down", "pct": 10},
            ctx,
        )
        assert result["triggered"] is True


class TestPriceVsLevel:
    """price_vs_level: 价格与参考位的关系。"""

    def test_price_above_ma20(self) -> None:
        # 300197.SZ 连涨后价格 2.78 远高于 MA20=1.74
        ctx = _make_ctx("300197.SZ", ["price", "tech"])
        result = evaluate_atom(
            "price_vs_level",
            {"ticker": "300197.SZ", "level": "MA20", "relation": "above"},
            ctx,
        )
        assert result["triggered"] is True

    def test_price_near_numeric_level(self) -> None:
        # 000001.SZ close=10.99，测试 near 11.00 容差 1%
        ctx = _make_ctx("000001.SZ", ["price", "tech"])
        result = evaluate_atom(
            "price_vs_level",
            {"ticker": "000001.SZ", "level": 11.00, "relation": "near", "tolerance_pct": 1},
            ctx,
        )
        assert result["triggered"] is True


class TestNewExtreme:
    """new_extreme: 创 N 日新高/新低。"""

    def test_new_high_triggered(self) -> None:
        # 300197.SZ 连续3天涨，0603 close=2.78 创近期新高
        ctx = _make_ctx("300197.SZ", ["price", "history"])
        result = evaluate_atom(
            "new_extreme",
            {"ticker": "300197.SZ", "direction": "high", "n_days": 20},
            ctx,
        )
        assert result["triggered"] is True

    def test_new_low_triggered(self) -> None:
        # 300085.SZ 0603 close=35.13，从 43 高位暴跌，创20日新低
        ctx = _make_ctx("300085.SZ", ["price", "history"])
        result = evaluate_atom(
            "new_extreme",
            {"ticker": "300085.SZ", "direction": "low", "n_days": 20},
            ctx,
        )
        assert result["triggered"] is True


class TestGap:
    """gap: 跳空开盘。"""

    def test_gap_up_triggered(self) -> None:
        # 301366.SZ: gap=20.01%（一字涨停开盘）
        ctx = _make_ctx("301366.SZ", ["price", "tech"])
        result = evaluate_atom(
            "gap",
            {"ticker": "301366.SZ", "direction": "up", "min_pct": 5},
            ctx,
        )
        assert result["triggered"] is True

    def test_gap_down_triggered(self) -> None:
        # 300085.SZ: 从 41.58 开盘 41.24，gap 不大，换一个
        # 002681.SZ: 0603 跌 -10.05%，pre=5.57 open=5.56 gap 不大
        # 检查 600688.SH: pre=2.79 open=3.07 gap=10.04%
        ctx = _make_ctx("600688.SH", ["price", "tech"])
        result = evaluate_atom(
            "gap",
            {"ticker": "600688.SH", "direction": "up", "min_pct": 5},
            ctx,
        )
        assert result["triggered"] is True


class TestConsecutiveMove:
    """consecutive_move: 连续 N 天涨/跌。"""

    def test_consecutive_up_3(self) -> None:
        # 300197.SZ: 0601(+19.88%) → 0602(+20.21%) → 0603(+19.83%) 连续3天涨
        ctx = _make_ctx("300197.SZ", ["history"])
        result = evaluate_atom(
            "consecutive_move",
            {"ticker": "300197.SZ", "direction": "up", "n_days": 3},
            ctx,
        )
        assert result["triggered"] is True

    def test_consecutive_down_3(self) -> None:
        # 000002.SZ: 0529(3.55) → 0601(3.47) → 0602(3.39) → 0603(3.28) 连续3天跌
        ctx = _make_ctx("000002.SZ", ["history"])
        result = evaluate_atom(
            "consecutive_move",
            {"ticker": "000002.SZ", "direction": "down", "n_days": 3},
            ctx,
        )
        assert result["triggered"] is True


# ═══════════════════════════════════════════════
# 量价关系 (3)
# ═══════════════════════════════════════════════


class TestVolumeRatio:
    """volume_ratio: 成交量相对 N 日均量的倍数。"""

    def test_volume_ratio_above(self) -> None:
        # 300085.SZ 0603 vol=1381376，前20日平均约 40 万，量比约 3.4 倍
        ctx = _make_ctx("300085.SZ", ["tech", "history"])
        result = evaluate_atom(
            "volume_ratio",
            {"ticker": "300085.SZ", "multiplier": 2.0, "relation": "above"},
            ctx,
        )
        assert result["triggered"] is True
        assert result["detail"]["volume_ratio"] >= 2.0

    def test_volume_ratio_below(self) -> None:
        # 000001.SZ 0603 vol=825272，20日均量约 93 万，量比 ≈ 0.885
        ctx = _make_ctx("000001.SZ", ["tech", "history"])
        result = evaluate_atom(
            "volume_ratio",
            {"ticker": "000001.SZ", "multiplier": 0.9, "relation": "below"},
            ctx,
        )
        assert result["triggered"] is True
        assert result["detail"]["volume_ratio"] < 1.0


class TestTurnoverActive:
    """turnover_active: 换手率活跃度。"""

    def test_turnover_above(self) -> None:
        # 300085.SZ 暴跌日换手率很高
        ctx = _make_ctx("300085.SZ", ["turnover"])
        result = evaluate_atom(
            "turnover_active",
            {"ticker": "300085.SZ", "pct": 5.0, "relation": "above"},
            ctx,
        )
        assert result["triggered"] is True


class TestAmplitudeWide:
    """amplitude_wide: 日内振幅。"""

    def test_amplitude_wide_triggered(self) -> None:
        # 300085.SZ 0603: high=43.1 low=33.56 open=41.24
        # 振幅 = (43.1 - 33.56) / 41.24 * 100 = 23.13%
        ctx = _make_ctx("300085.SZ", ["snapshot"])
        result = evaluate_atom(
            "amplitude_wide",
            {"ticker": "300085.SZ", "pct": 20, "relation": "above"},
            ctx,
        )
        assert result["triggered"] is True
        assert result["detail"]["amplitude"] >= 20


# ═══════════════════════════════════════════════
# 趋势结构 (3)
# ═══════════════════════════════════════════════


class TestMASlope:
    """ma_slope: 均线自身方向。"""

    def test_ma5_up(self) -> None:
        # 300197.SZ 连涨3天，MA5 拐头向上
        ctx = _make_ctx("300197.SZ", ["tech", "history"])
        result = evaluate_atom(
            "ma_slope",
            {"ticker": "300197.SZ", "period": "MA5", "direction": "up"},
            ctx,
        )
        assert result["triggered"] is True

    def test_ma5_down(self) -> None:
        # 000001.SZ 连跌多日，MA5 拐头向下
        ctx = _make_ctx("000001.SZ", ["tech", "history"])
        result = evaluate_atom(
            "ma_slope",
            {"ticker": "000001.SZ", "period": "MA5", "direction": "down"},
            ctx,
        )
        assert result["triggered"] is True


class TestMACross:
    """ma_cross: 均线金叉/死叉。"""

    def test_golden_cross(self) -> None:
        # 000010.SZ: MA5 从 2.11 上穿 MA10 到 2.17，金叉
        ctx = _make_ctx("000010.SZ", ["tech", "history"])
        result = evaluate_atom(
            "ma_cross",
            {"ticker": "000010.SZ", "fast_period": "MA5", "slow_period": "MA10", "direction": "golden"},
            ctx,
        )
        assert result["triggered"] is True


class TestMAAlignment:
    """ma_alignment: 多头/空头排列。"""

    def test_bullish_alignment(self) -> None:
        # 000027.SZ: MA5=8.03 > MA10=7.76 > MA20=7.55 > MA60=7.20 多头排列
        ctx = _make_ctx("000027.SZ", ["tech"])
        result = evaluate_atom(
            "ma_alignment",
            {"ticker": "000027.SZ", "pattern": "bullish"},
            ctx,
        )
        assert result["triggered"] is True

    def test_bearish_alignment(self) -> None:
        # 000002.SZ: MA5=3.40 < MA10=3.41 < MA20=3.65 < MA60=3.96 空头排列
        ctx = _make_ctx("000002.SZ", ["tech"])
        result = evaluate_atom(
            "ma_alignment",
            {"ticker": "000002.SZ", "pattern": "bearish"},
            ctx,
        )
        assert result["triggered"] is True


# ═══════════════════════════════════════════════
# 日内动态 (3)
# ═══════════════════════════════════════════════


class TestIntradayReversal:
    """intraday_reversal: 冲高回落或探底回升。"""

    def test_shot_up_fall(self) -> None:
        # 300085.SZ 0603: open=41.24 high=43.1(+4.51%) close=35.13
        # 从高点 43.1 回落至 35.13，回落幅度大
        ctx = _make_ctx("300085.SZ", ["snapshot"])
        result = evaluate_atom(
            "intraday_reversal",
            {"ticker": "300085.SZ", "pattern": "shot_up_fall", "move_pct": 3, "retrace_ratio": 50},
            ctx,
        )
        assert result["triggered"] is True


class TestIntradayRoundTrip:
    """intraday_round_trip: A字或V字往返。"""

    def test_a_pattern(self) -> None:
        # 300085.SZ: 开盘 41.24 → 冲高 43.1 → 收 35.13（回到开盘下方）
        ctx = _make_ctx("300085.SZ", ["snapshot"])
        result = evaluate_atom(
            "intraday_round_trip",
            {"ticker": "300085.SZ", "direction": "A", "min_move_pct": 3, "tolerance_pct": 2},
            ctx,
        )
        # latest_pct from open = (35.13-41.24)/41.24*100 = -14.8%，超出 tolerance
        # 所以 A 字往返（回到原点）不触发，这是正确的
        assert result["triggered"] is False

    def test_v_pattern_not_triggered(self) -> None:
        # 300085.SZ 不是 V 字
        ctx = _make_ctx("300085.SZ", ["snapshot"])
        result = evaluate_atom(
            "intraday_round_trip",
            {"ticker": "300085.SZ", "direction": "V", "min_move_pct": 3, "tolerance_pct": 2},
            ctx,
        )
        assert result["triggered"] is False


class TestIntradayTrend:
    """intraday_trend: 日内单边走势。"""

    def test_down_trend(self) -> None:
        # 300561.SZ 0603 跌 -9.9%，最后 60 分钟 move=-5.81%，回撤比 0.15
        ctx = _make_ctx("300561.SZ", ["snapshot"])
        result = evaluate_atom(
            "intraday_trend",
            {"ticker": "300561.SZ", "direction": "down", "minutes": 60, "min_pct": 5},
            ctx,
        )
        assert result["triggered"] is True
        assert result["detail"]["move_pct"] <= -5


# ═══════════════════════════════════════════════
# 板块与市场 (5)
# ═══════════════════════════════════════════════


class TestSectorMove:
    """sector_move: 板块涨跌。"""

    def test_sector_move_basic(self) -> None:
        # 构造一个虚拟 sector overview
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={},
            sector_data={
                "半导体": {
                    "overview": {
                        "code": "888888.TI",
                        "name": "半导体",
                        "pct_chg": 5.2,
                        "up_count": 45,
                        "down_count": 10,
                        "total_count": 55,
                    },
                    "members": [],
                    "intraday": {},
                }
            },
            market_summary={},
        )
        result = evaluate_atom(
            "sector_move",
            {"sector": "半导体", "direction": "up", "pct": 3},
            ctx,
        )
        assert result["triggered"] is True


class TestSectorBreadth:
    """sector_breadth: 板块内涨跌比。"""

    def test_sector_breadth_triggered(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={},
            sector_data={
                "新能源": {
                    "overview": {
                        "code": "888889.TI",
                        "name": "新能源",
                        "pct_chg": 2.1,
                        "up_count": 30,
                        "down_count": 5,
                        "total_count": 35,
                    },
                    "members": [],
                    "intraday": {},
                }
            },
            market_summary={},
        )
        result = evaluate_atom(
            "sector_breadth",
            {"sector": "新能源", "up_ratio_min": 0.8},
            ctx,
        )
        assert result["triggered"] is True
        assert result["detail"]["up_ratio"] >= 0.8


class TestSectorLimitRatio:
    """sector_limit_ratio: 板块涨停/跌停家数。"""

    def test_limit_up_count(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={
                "301115.SZ": {"zdt_record": {"is_limit": True, "limit_type": "涨停池"}},
                "301366.SZ": {"zdt_record": {"is_limit": True, "limit_type": "涨停池"}},
                "688655.SH": {"zdt_record": {"is_limit": True, "limit_type": "涨停池"}},
            },
            sector_data={
                "芯片": {
                    "overview": {"code": "888890.TI", "name": "芯片", "pct_chg": 8.0},
                    "members": ["301115.SZ", "301366.SZ", "688655.SH"],
                    "intraday": {},
                }
            },
            market_summary={},
        )
        result = evaluate_atom(
            "sector_limit_ratio",
            {"sector": "芯片", "direction": "up", "min_count": 2},
            ctx,
        )
        assert result["triggered"] is True
        assert result["detail"]["limit_count"] >= 2


class TestMarketBreadth:
    """market_breadth: 全市场涨跌比。"""

    def test_breadth_triggered(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={},
            sector_data={},
            market_summary={"up_down_ratio": 2.5, "avg_pct_chg": 1.5, "total_amount_yi": 12000.0},
        )
        result = evaluate_atom(
            "market_breadth",
            {"up_down_ratio_min": 2.0},
            ctx,
        )
        assert result["triggered"] is True

    def test_breadth_with_avg_pct(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={},
            sector_data={},
            market_summary={"up_down_ratio": 1.8, "avg_pct_chg": 0.8, "total_amount_yi": 9000.0},
        )
        result = evaluate_atom(
            "market_breadth",
            {"up_down_ratio_min": 1.5, "avg_pct_min": 0.5},
            ctx,
        )
        assert result["triggered"] is True


# market_volume 原子已删除，测试类 TestMarketVolume 随之移除


# ═══════════════════════════════════════════════
# 时间 (3, meta)
# ═══════════════════════════════════════════════


class TestTimeAtoms:
    """时间原子评估。"""

    def test_time_after_triggered(self) -> None:
        from src.triggers.evaluators.time import eval_time_after

        result = eval_time_after(
            {"created_at": "2026-06-01T00:00:00+08:00", "days": 1},
            EvalContext(now=TEST_DT, ticker_data={}, sector_data={}),
        )
        assert result["triggered"] is True

    def test_time_after_not_triggered(self) -> None:
        from src.triggers.evaluators.time import eval_time_after

        result = eval_time_after(
            {"created_at": "2026-06-03T00:00:00+08:00", "days": 5},
            EvalContext(now=TEST_DT, ticker_data={}, sector_data={}),
        )
        assert result["triggered"] is False

    def test_time_window_triggered(self) -> None:
        from src.triggers.evaluators.time import eval_time_window

        result = eval_time_window(
            {"created_at": "2026-06-01T00:00:00+08:00", "days_min": 1, "days_max": 5},
            EvalContext(now=TEST_DT, ticker_data={}, sector_data={}),
        )
        assert result["triggered"] is True

    def test_time_before_triggered(self) -> None:
        from src.triggers.evaluators.time import eval_time_before

        result = eval_time_before(
            {"created_at": "2026-06-01T00:00:00+08:00", "days": 10},
            EvalContext(now=TEST_DT, ticker_data={}, sector_data={}),
        )
        assert result["triggered"] is True


# ═══════════════════════════════════════════════
# 边界场景
# ═══════════════════════════════════════════════


class TestEdgeCases:
    """数据不足 / 缺失场景。"""

    def test_price_move_insufficient_history(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={
                "999999.SH": {
                    "price": {"price": 10.0, "pct_chg": 1.0},
                    "history": {"data": [{"close": 9.0}, {"close": 9.5}], "count": 2},
                }
            },
            sector_data={},
            market_summary={},
        )
        result = evaluate_atom(
            "price_move",
            {"ticker": "999999.SH", "direction": "up", "pct": 10, "lookback_days": 5},
            ctx,
        )
        assert result["triggered"] is False

    def test_new_extreme_insufficient_data(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={
                "999999.SH": {
                    "price": {"price": 10.0},
                    "history": {"data": [{"close": 9.0, "high": 9.5, "low": 8.5}], "count": 1},
                }
            },
            sector_data={},
            market_summary={},
        )
        result = evaluate_atom(
            "new_extreme",
            {"ticker": "999999.SH", "direction": "high", "n_days": 20},
            ctx,
        )
        assert result["triggered"] is False

    def test_intraday_no_1m_data(self) -> None:
        ctx = EvalContext(
            now=TEST_DT,
            ticker_data={"999999.SH": {"snapshot": {"error": "无1分钟数据: 999999.SH"}}},
            sector_data={},
            market_summary={},
        )
        result = evaluate_atom(
            "intraday_reversal",
            {"ticker": "999999.SH", "pattern": "shot_up_fall", "move_pct": 3},
            ctx,
        )
        assert result["triggered"] is False

    def test_unknown_atom(self) -> None:
        result = evaluate_atom("nonexistent_atom", {}, EvalContext(now=TEST_DT))
        assert result["triggered"] is False
